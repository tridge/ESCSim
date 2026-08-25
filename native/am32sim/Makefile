CC ?= cc
BUILD_DIR ?= ../../build/native
CONFIGURATION ?= Release

SOURCES := \
	src/am32sim_shim.c \
	src/jsmn.c \
	src/motor.c \
	src/sitl_config.c
OBJECTS := $(patsubst src/%.c,$(BUILD_DIR)/obj/%.o,$(SOURCES))
DEPS := $(OBJECTS:.o=.d)

CPPFLAGS += -Iinclude -D_DEFAULT_SOURCE
CFLAGS += -std=gnu11 -Wall -Wextra -Wno-unused-parameter -MMD -MP
THREAD_LIBS := -pthread

ifeq ($(CONFIGURATION),Release)
CFLAGS += -O3 -DNDEBUG
else ifeq ($(CONFIGURATION),Debug)
CFLAGS += -O0 -g3
else
$(error CONFIGURATION must be Release or Debug)
endif

ifeq ($(OS),Windows_NT)
LIBRARY := $(BUILD_DIR)/am32sim.dll
IMPORT_LIBRARY := $(BUILD_DIR)/libam32sim.dll.a
SMOKE := $(BUILD_DIR)/am32sim_smoke.exe
SHARED_FLAGS := -shared -Wl,--export-all-symbols \
	-Wl,--out-implib,$(IMPORT_LIBRARY)
THREAD_LIBS := -Wl,-Bstatic -lwinpthread -Wl,-Bdynamic
RPATH_FLAGS :=
PIC_FLAGS :=
else
UNAME_S := $(shell uname -s)
PIC_FLAGS := -fPIC
ifeq ($(UNAME_S),Darwin)
LIBRARY := $(BUILD_DIR)/libam32sim.dylib
SHARED_FLAGS := -dynamiclib -Wl,-install_name,@rpath/libam32sim.dylib
RPATH_FLAGS := -Wl,-rpath,@loader_path
else
LIBRARY := $(BUILD_DIR)/libam32sim.so
SHARED_FLAGS := -shared
RPATH_FLAGS := -Wl,-rpath,'$$ORIGIN'
endif
SMOKE := $(BUILD_DIR)/am32sim_smoke
endif

LDLIBS += $(THREAD_LIBS) -lm
CONFIG_STAMP := $(BUILD_DIR)/.$(CONFIGURATION)

.DEFAULT_GOAL := all
.PHONY: all smoke test clean

all: $(LIBRARY)

smoke: $(SMOKE)

test: $(SMOKE)
	$(SMOKE)

$(LIBRARY): $(OBJECTS)
	@mkdir -p $(@D)
	$(CC) $(SHARED_FLAGS) -o $@ $^ $(LDFLAGS) $(LDLIBS)

$(SMOKE): tests/smoke.c $(LIBRARY)
	@mkdir -p $(@D)
	$(CC) $(CPPFLAGS) $(CFLAGS) -o $@ $< -L$(BUILD_DIR) -lam32sim \
		$(LDFLAGS) $(RPATH_FLAGS) $(LDLIBS)

$(BUILD_DIR)/obj/%.o: src/%.c $(CONFIG_STAMP)
	@mkdir -p $(@D)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(PIC_FLAGS) -c -o $@ $<

clean:
	$(RM) $(OBJECTS) $(DEPS) $(LIBRARY) $(IMPORT_LIBRARY) $(SMOKE) \
		$(BUILD_DIR)/.Release $(BUILD_DIR)/.Debug

$(CONFIG_STAMP):
	@mkdir -p $(@D)
	$(RM) $(BUILD_DIR)/.Release $(BUILD_DIR)/.Debug
	@touch $@

-include $(DEPS)
