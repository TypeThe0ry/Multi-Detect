#pragma once
#include <cinttypes>
#include <cstdio>
#include <unistd.h>
inline int system_usleep(unsigned us) { return usleep(us); }
#define PX4_INFO(...) do {} while(0)
#define PX4_DEBUG(...) do {} while(0)
#define PX4_ERR(...) do { fprintf(stderr, __VA_ARGS__); fputc('\n',stderr); } while(0)
