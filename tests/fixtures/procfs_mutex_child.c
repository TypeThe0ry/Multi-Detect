#define _GNU_SOURCE
#include <dlfcn.h>
#include <gnu/libc-version.h>
#include <pthread.h>
#include <stddef.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <signal.h>
#include <sys/prctl.h>
static pthread_mutex_t mutex;
static _Atomic int ready=0;
static pid_t owner_tid;
static void *owner(void *unused) {
 (void)unused;
 pthread_mutex_lock(&mutex); pthread_mutex_lock(&mutex);
 owner_tid=(pid_t)syscall(SYS_gettid); atomic_store(&ready,1);
 usleep(1800000);
 pthread_mutex_unlock(&mutex); pthread_mutex_unlock(&mutex);
 return NULL;
}
int main(void) {
 alarm(7); // Own child only; normally returns cleanly in <2s.
 pthread_mutexattr_t attr; pthread_mutexattr_init(&attr);
 pthread_mutexattr_settype(&attr,PTHREAD_MUTEX_RECURSIVE);
 pthread_mutex_init(&mutex,&attr); pthread_mutexattr_destroy(&attr);
 pthread_t thread; if(pthread_create(&thread,NULL,owner,NULL)) return 2;
 while(!atomic_load(&ready)) usleep(1000);
 Dl_info info; if(!dladdr((void *)gnu_get_libc_version,&info)) return 3;
 if(getenv("PROBE_DENY") && prctl(PR_SET_DUMPABLE,0)) return 4;
 printf("{\"pid\":%d,\"main_tid\":%ld,\"owner_tid\":%d,\"mutex_address\":\"0x%lx\",\"glibc_version\":\"%s\",\"libc_path\":\"%s\",\"sizeof_mutex\":%zu,\"alignof_mutex\":%zu,\"offsets\":{\"lock\":%zu,\"count\":%zu,\"owner\":%zu,\"nusers\":%zu,\"kind\":%zu},\"recursive_kind\":%d}\n",getpid(),syscall(SYS_gettid),owner_tid,(unsigned long)(uintptr_t)&mutex,gnu_get_libc_version(),info.dli_fname,sizeof(mutex),__alignof__(pthread_mutex_t),offsetof(pthread_mutex_t,__data.__lock),offsetof(pthread_mutex_t,__data.__count),offsetof(pthread_mutex_t,__data.__owner),offsetof(pthread_mutex_t,__data.__nusers),offsetof(pthread_mutex_t,__data.__kind),mutex.__data.__kind);
 fflush(stdout);
 pthread_mutex_lock(&mutex); pthread_mutex_unlock(&mutex);
 pthread_join(thread,NULL); pthread_mutex_destroy(&mutex);
 puts("RELEASED_NORMALLY"); return 0;
}
