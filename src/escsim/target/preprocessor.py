"""A bounded C-preprocessor subset for AM32's self-contained targets.h.

This is deliberately not a general C preprocessor. It implements the
conditional and object-macro directives used by targets.h, never opens an
include, and returns raw final macro definitions in the same form needed by
the Renode generator. CI compares its output with GCC for every supported
target.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


_DIRECTIVE = re.compile(r"^\s*#\s*([A-Za-z_][A-Za-z0-9_]*)\b(.*)$")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TOKEN = re.compile(
    r"\s*(defined|&&|\|\||==|!=|<=|>=|[!()<>]|"
    r"0[xX][0-9A-Fa-f]+[uUlL]*|[0-9]+[uUlL]*|[A-Za-z_][A-Za-z0-9_]*)"
)


class PreprocessorError(ValueError):
    """targets.h uses unsupported or malformed preprocessing input."""


@dataclass
class _Conditional:
    parent_active: bool
    branch_taken: bool
    active: bool
    else_seen: bool = False


def _without_comments(text: str) -> str:
    """Remove C comments while preserving strings, characters, and lines."""

    output: list[str] = []
    index = 0
    state = "code"
    quote = ""
    while index < len(text):
        char = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if state == "line-comment":
            if char == "\n":
                output.append(char)
                state = "code"
            else:
                output.append(" ")
            index += 1
            continue
        if state == "block-comment":
            if char == "*" and following == "/":
                output.extend("  ")
                index += 2
                state = "code"
            else:
                output.append("\n" if char == "\n" else " ")
                index += 1
            continue
        if state == "quote":
            output.append(char)
            if char == "\\" and following:
                output.append(following)
                index += 2
                continue
            if char == quote:
                state = "code"
            index += 1
            continue
        if char == "/" and following == "/":
            output.extend("  ")
            index += 2
            state = "line-comment"
        elif char == "/" and following == "*":
            output.extend("  ")
            index += 2
            state = "block-comment"
        else:
            output.append(char)
            if char in {'"', "'"}:
                state = "quote"
                quote = char
            index += 1
    if state == "block-comment":
        raise PreprocessorError("unterminated block comment")
    if state == "quote":
        raise PreprocessorError("unterminated quoted value")
    return "".join(output)


def _integer_value(value: str) -> int:
    token = value.strip().split(None, 1)[0] if value.strip() else "0"
    token = re.sub(r"[uUlL]+$", "", token)
    try:
        return int(token, 0)
    except ValueError:
        return 0


class _Expression:
    def __init__(self, expression: str, macros: dict[str, str]) -> None:
        self.macros = macros
        self.tokens: list[str] = []
        position = 0
        while position < len(expression):
            match = _TOKEN.match(expression, position)
            if not match:
                if expression[position:].strip():
                    raise PreprocessorError(
                        f"unsupported conditional expression: {expression.strip()}"
                    )
                break
            self.tokens.append(match.group(1))
            position = match.end()
        self.position = 0

    def _peek(self) -> str | None:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def _take(self, token: str | None = None) -> str:
        value = self._peek()
        if value is None or (token is not None and value != token):
            raise PreprocessorError(f"expected {token or 'expression token'}")
        self.position += 1
        return value

    def parse(self) -> bool:
        result = self._or()
        if self._peek() is not None:
            raise PreprocessorError(f"unexpected conditional token {self._peek()}")
        return bool(result)

    def _or(self) -> int:
        value = self._and()
        while self._peek() == "||":
            self._take()
            right = self._and()
            value = int(bool(value) or bool(right))
        return value

    def _and(self) -> int:
        value = self._comparison()
        while self._peek() == "&&":
            self._take()
            right = self._comparison()
            value = int(bool(value) and bool(right))
        return value

    def _comparison(self) -> int:
        left = self._unary()
        operator = self._peek()
        if operator not in {"==", "!=", "<", ">", "<=", ">="}:
            return left
        self._take()
        right = self._unary()
        return int(
            {
                "==": left == right,
                "!=": left != right,
                "<": left < right,
                ">": left > right,
                "<=": left <= right,
                ">=": left >= right,
            }[operator]
        )

    def _unary(self) -> int:
        if self._peek() == "!":
            self._take()
            return int(not self._unary())
        if self._peek() == "defined":
            self._take()
            if self._peek() == "(":
                self._take("(")
                name = self._take()
                self._take(")")
            else:
                name = self._take()
            if not _IDENTIFIER.fullmatch(name):
                raise PreprocessorError(f"invalid defined() name {name}")
            return int(name in self.macros)
        if self._peek() == "(":
            self._take()
            value = self._or()
            self._take(")")
            return value
        token = self._take()
        if _IDENTIFIER.fullmatch(token):
            return _integer_value(self.macros.get(token, "0"))
        return _integer_value(token)


def _condition(expression: str, macros: dict[str, str]) -> bool:
    return _Expression(expression, macros).parse()


def preprocess_macros(text: str, target: str) -> dict[str, str]:
    """Return final object-macro definitions for one AM32 target."""

    if not _IDENTIFIER.fullmatch(target):
        raise PreprocessorError(f"invalid target macro name: {target!r}")
    macros: dict[str, str] = {target: ""}
    stack: list[_Conditional] = []
    active = True
    for line_number, line in enumerate(_without_comments(text).splitlines(), 1):
        match = _DIRECTIVE.match(line)
        if not match:
            continue
        directive = match.group(1)
        body = match.group(2).strip()
        try:
            if directive in {"ifdef", "ifndef", "if"}:
                parent_active = active
                if directive == "if":
                    selected = _condition(body, macros) if parent_active else False
                else:
                    name_match = _IDENTIFIER.fullmatch(body)
                    if not name_match:
                        raise PreprocessorError(f"invalid #{directive} name")
                    selected = body in macros
                    if directive == "ifndef":
                        selected = not selected
                    selected = parent_active and selected
                frame = _Conditional(parent_active, selected, selected)
                stack.append(frame)
                active = frame.active
            elif directive == "elif":
                if not stack:
                    raise PreprocessorError("#elif without #if")
                frame = stack[-1]
                if frame.else_seen:
                    raise PreprocessorError("#elif after #else")
                selected = (
                    frame.parent_active
                    and not frame.branch_taken
                    and _condition(body, macros)
                )
                frame.active = selected
                frame.branch_taken = frame.branch_taken or selected
                active = selected
            elif directive == "else":
                if not stack:
                    raise PreprocessorError("#else without #if")
                frame = stack[-1]
                if frame.else_seen:
                    raise PreprocessorError("duplicate #else")
                frame.else_seen = True
                selected = frame.parent_active and not frame.branch_taken
                frame.active = selected
                frame.branch_taken = frame.branch_taken or selected
                active = selected
            elif directive == "endif":
                if not stack:
                    raise PreprocessorError("#endif without #if")
                stack.pop()
                active = stack[-1].active if stack else True
            elif directive == "define" and active:
                name_match = _IDENTIFIER.match(body)
                if not name_match:
                    raise PreprocessorError("invalid #define")
                name = name_match.group(0)
                remainder = body[name_match.end() :]
                if remainder.startswith("("):
                    macros[name] = ""
                else:
                    macros[name] = remainder.strip()
            elif directive == "undef" and active:
                if not _IDENTIFIER.fullmatch(body):
                    raise PreprocessorError("invalid #undef")
                macros.pop(body, None)
            elif directive in {"include", "include_next", "import", "embed"}:
                raise PreprocessorError(f"#{directive} is not permitted")
            elif directive == "error":
                if active:
                    raise PreprocessorError(body or "active #error")
            elif directive == "pragma":
                if body.lower() != "once":
                    raise PreprocessorError(f"unsupported #pragma {body}")
            elif directive not in {"define", "undef"}:
                raise PreprocessorError(f"unsupported directive #{directive}")
        except PreprocessorError as error:
            raise PreprocessorError(f"targets.h:{line_number}: {error}") from error
    if stack:
        raise PreprocessorError("targets.h: unterminated conditional")
    return macros
