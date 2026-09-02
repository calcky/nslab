CLANG ?= clang
CPPFLAGS += -I/usr/include/$(shell uname -m)-linux-gnu
CFLAGS += -O2 -g -target bpf -Wall -Werror

.PHONY: all clean

all: xdp_lab.o

xdp_lab.o: xdp_lab.c
	$(CLANG) $(CPPFLAGS) $(CFLAGS) -c $< -o $@

clean:
	rm -f xdp_lab.o
