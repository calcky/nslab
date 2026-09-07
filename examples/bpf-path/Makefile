CLANG ?= clang
CPPFLAGS += -I/usr/include/$(shell uname -m)-linux-gnu
CFLAGS += -O2 -g -target bpf -Wall -Werror

.PHONY: all redirect cpumap af-xdp advanced clean
all: path_lab.o
redirect: redirect_lab.bpf.o redirect_lab
cpumap: cpu_lab.bpf.o cpu_lab
af-xdp: xsk_lab.bpf.o xsk_lab
advanced: redirect cpumap af-xdp
path_lab.o: path_lab.c
	$(CLANG) $(CPPFLAGS) $(CFLAGS) -c $< -o $@
redirect_lab.bpf.o: redirect_lab.bpf.c packet.bpf.h
	$(CLANG) $(CPPFLAGS) $(CFLAGS) -c $< -o $@
redirect_lab: redirect_lab.c
	$(CC) $(CPPFLAGS) -O2 -g -Wall -Wextra -Werror $< -o $@ -lbpf -lelf -lz
cpu_lab.bpf.o: cpu_lab.bpf.c packet.bpf.h
	$(CLANG) $(CPPFLAGS) $(CFLAGS) -c $< -o $@
xsk_lab.bpf.o: xsk_lab.bpf.c packet.bpf.h
	$(CLANG) $(CPPFLAGS) $(CFLAGS) -c $< -o $@
cpu_lab: cpu_lab.c xdp_common.h
	$(CC) $(CPPFLAGS) -O2 -g -Wall -Wextra -Werror $< -o $@ -lbpf -lelf -lz
xsk_lab: xsk_lab.c xdp_common.h echo_reply.h
	$(CC) $(CPPFLAGS) -O2 -g -Wall -Wextra -Werror $< -o $@ $(LDFLAGS) -lxdp -lbpf -lelf -lz
clean:
	rm -f path_lab.o redirect_lab.bpf.o redirect_lab cpu_lab.bpf.o cpu_lab xsk_lab.bpf.o xsk_lab
