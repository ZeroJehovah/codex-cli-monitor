CC = x86_64-w64-mingw32-gcc
WINDRES = x86_64-w64-mingw32-windres
CFLAGS = -Wall -Wextra -O2 -DUNICODE -D_UNICODE -Imingw-sysroot/x86_64-w64-mingw32/include
LDFLAGS = -mwindows -lwinhttp -lws2_32
TARGET = CodexMonitorWidget.exe
BUILDDIR = build
SRCDIR = src

SOURCES = $(SRCDIR)/main.c $(SRCDIR)/websocket.c
OBJECTS = $(BUILDDIR)/main.o $(BUILDDIR)/websocket.o $(BUILDDIR)/resources.o

all: $(BUILDDIR) $(TARGET)

$(BUILDDIR):
	mkdir -p $(BUILDDIR)

$(BUILDDIR)/main.o: $(SRCDIR)/main.c $(SRCDIR)/websocket.h
	$(CC) $(CFLAGS) -c $< -o $@

$(BUILDDIR)/websocket.o: $(SRCDIR)/websocket.c $(SRCDIR)/websocket.h
	$(CC) $(CFLAGS) -c $< -o $@

$(BUILDDIR)/resources.o: $(SRCDIR)/resources.rc
	$(WINDRES) $< $@

$(TARGET): $(OBJECTS)
	$(CC) $(OBJECTS) $(LDFLAGS) -o $@

clean:
	rm -rf $(BUILDDIR) $(TARGET)

.PHONY: all clean
