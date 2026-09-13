BINUTILS_DIR ?=
MINGW_INCLUDE_DIR ?=
ifeq ($(origin CC),default)
CC := $(shell command -v x86_64-w64-mingw32-gcc-posix 2>/dev/null || command -v x86_64-w64-mingw32-gcc 2>/dev/null || printf '%s' 'x86_64-w64-mingw32-gcc-posix')
endif
ifeq ($(origin WINDRES),undefined)
WINDRES := $(shell command -v x86_64-w64-mingw32-windres 2>/dev/null || true)
endif
CFLAGS = -Wall -Wextra -O2 -DUNICODE -D_UNICODE
ifneq ($(strip $(MINGW_INCLUDE_DIR)),)
CFLAGS += -I$(MINGW_INCLUDE_DIR)
endif
LDFLAGS = -mwindows -municode -lwinhttp -lcomctl32 -lshell32 -luser32 -lgdi32 -ladvapi32 -lwinmm -lmsimg32 -lws2_32
ifneq ($(strip $(BINUTILS_DIR)),)
CFLAGS += -B$(BINUTILS_DIR)/
LDFLAGS += -B$(BINUTILS_DIR)/
endif
TARGET = CodexMonitorWidget.exe
BUILDDIR = build
SRCDIR = src

SOURCES = $(SRCDIR)/main.c $(SRCDIR)/websocket.c
OBJECTS = $(BUILDDIR)/main.o $(BUILDDIR)/websocket.o

ifneq ($(strip $(WINDRES)),)
OBJECTS += $(BUILDDIR)/resources.o
endif

all: $(BUILDDIR) $(TARGET)

$(BUILDDIR):
	mkdir -p $(BUILDDIR)

$(BUILDDIR)/main.o: $(SRCDIR)/main.c $(SRCDIR)/websocket.h
	$(CC) $(CFLAGS) -c $< -o $@

$(BUILDDIR)/websocket.o: $(SRCDIR)/websocket.c $(SRCDIR)/websocket.h
	$(CC) $(CFLAGS) -c $< -o $@

ifneq ($(strip $(WINDRES)),)
$(BUILDDIR)/resources.o: $(SRCDIR)/resources.rc
	$(WINDRES) $< $@
endif

$(TARGET): $(OBJECTS)
	$(CC) $(OBJECTS) $(LDFLAGS) -o $@

clean:
	rm -rf $(BUILDDIR) $(TARGET)

.PHONY: all clean
