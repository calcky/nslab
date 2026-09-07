CLANG ?= clang
CPPFLAGS += -I/usr/include/$(shell uname -m)-linux-gnu
CFLAGS += -O2 -g -target bpf -Wall -Werror

.PHONY: all clean
all: path_lab.o
path_lab.o: path_lab.c
	$(CLANG) $(CPPFLAGS) $(CFLAGS) -c $< -o $@
clean:
	rm -f path_lab.o
