#pragma once
#include <cstdint>
#include <time.h>
inline uint64_t hrt_absolute_time() { timespec ts; clock_gettime(CLOCK_MONOTONIC,&ts); return uint64_t(ts.tv_sec)*1000000+ts.tv_nsec/1000; }
