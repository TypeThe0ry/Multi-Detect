#include <zmq.h>
#include <dlfcn.h>
#include <atomic>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
static std::atomic<int> calls{0},injected{0};
extern "C" int zmq_msg_recv(zmq_msg_t *msg,void *socket,int flags)
{
  using Fn=int(*)(zmq_msg_t *,void *,int);static auto real=reinterpret_cast<Fn>(dlsym(RTLD_NEXT,"zmq_msg_recv"));
  int n=++calls;const char *mode=std::getenv("DELTA_RECV_FAULT");
  if(mode&&n==2){++injected;errno=std::strcmp(mode,"eintr")==0?EINTR:EAGAIN;return -1;}
  return real(msg,socket,flags);
}
__attribute__((destructor)) static void Report()
{
  const char *path=std::getenv("DELTA_FAULT_LOG");if(!path)return;
  if(FILE *f=std::fopen(path,"w")){std::fprintf(f,"{\"calls\":%d,\"injected\":%d}\n",calls.load(),injected.load());std::fclose(f);}
}
