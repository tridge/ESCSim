'''
locate the AM32 firmware checkout and the SITL binary built from it

The SITL simulator is C code in the AM32 firmware repo (Mcu/SITL); the
models, datasets, GUI and tests live here. Everything that needs to
reach into the firmware tree goes through this module so there is one
place that knows where it is.

The checkout is, in order:
  $AM32_ROOT                        - an explicit checkout, used by CI
                                      when testing a firmware branch
  ../modules/am32-firmware          - the pinned submodule
'''

import glob
import os

SITL_DIR = os.path.dirname(os.path.abspath(__file__))
ESCSIM_ROOT = os.path.dirname(SITL_DIR)

SUBMODULE = os.path.join(ESCSIM_ROOT, 'modules', 'am32-firmware')
BOOTLOADER_SUBMODULE = os.path.join(ESCSIM_ROOT, 'modules', 'am32-bootloader')

SITL_GLOB = 'AM32_AM32_SITL_CAN_*.elf'


def _is_am32(path):
    '''a checkout, rather than an empty submodule directory'''
    return os.path.isfile(os.path.join(path, 'Inc', 'version.h'))


def am32_root(required=True):
    '''the AM32 firmware checkout the SITL is built from'''
    env = os.environ.get('AM32_ROOT')
    if env:
        env = os.path.abspath(os.path.expanduser(env))
        if not _is_am32(env):
            raise SystemExit('AM32_ROOT=%s is not an AM32 checkout' % env)
        return env
    if _is_am32(SUBMODULE):
        return SUBMODULE
    if not required:
        return None
    raise SystemExit(
        'no AM32 checkout found: set AM32_ROOT, or run\n'
        '  git submodule update --init modules/am32-firmware')


def bootloader_root(required=False):
    '''the AM32 bootloader checkout, for SITL bootloader chain runs'''
    env = os.environ.get('AM32_BOOTLOADER_ROOT')
    path = os.path.abspath(os.path.expanduser(env)) if env \
        else BOOTLOADER_SUBMODULE
    if os.path.isdir(os.path.join(path, 'Mcu')):
        return path
    if not required:
        return None
    raise SystemExit(
        'no AM32 bootloader checkout found: set AM32_BOOTLOADER_ROOT, or run\n'
        '  git submodule update --init modules/am32-bootloader')


def obj_dir():
    '''where "make AM32_SITL_CAN" leaves its output'''
    return os.path.join(am32_root(), 'obj')


def sitl_binary(given=None, required=True):
    '''the built SITL binary: an explicit path, $AM32_SITL, or the
    newest one in the firmware checkout's obj/'''
    if given:
        if not os.path.isfile(given):
            raise SystemExit('SITL binary %s does not exist' % given)
        return given
    env = os.environ.get('AM32_SITL')
    if env:
        hits = sorted(glob.glob(env))
        if not hits:
            raise SystemExit('AM32_SITL=%s matches no file' % env)
        return hits[-1]
    root = am32_root(required=required)
    if root is None:
        return None
    hits = sorted(glob.glob(os.path.join(root, 'obj', SITL_GLOB)))
    if hits:
        return hits[-1]
    if not required:
        return None
    raise SystemExit(
        'no SITL binary in %s/obj: build it with\n'
        '  make -C %s AM32_SITL_CAN' % (root, root))


def data_dir(*parts):
    return os.path.join(SITL_DIR, 'data', *parts)


def models_dir(*parts):
    return os.path.join(SITL_DIR, 'models', *parts)


def scripts_dir(*parts):
    return os.path.join(SITL_DIR, 'scripts', *parts)
