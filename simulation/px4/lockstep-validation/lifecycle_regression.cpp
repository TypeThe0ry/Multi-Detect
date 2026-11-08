#include <lockstep_scheduler/lockstep_scheduler.h>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <thread>
#include <vector>
#include <cerrno>
using namespace std::chrono_literals;
extern "C" int __real_pthread_cond_wait(pthread_cond_t*,pthread_mutex_t*);
extern "C" int __real_pthread_cond_timedwait(pthread_cond_t*,pthread_mutex_t*,const timespec*);
extern "C" int __real_pthread_mutex_lock(pthread_mutex_t*);
std::atomic<int> entered{0},returned{0};
std::atomic<bool> pause_signal{false},signal_entered{false},release_signal{false};
pthread_mutex_t *target_lock=nullptr;
std::thread::id advancer_id;
extern "C" int __wrap_pthread_cond_wait(pthread_cond_t *c,pthread_mutex_t *m) {
    ++entered; return __real_pthread_cond_wait(c,m);
}
extern "C" int __wrap_pthread_cond_timedwait(pthread_cond_t *c,pthread_mutex_t *m,const timespec *t) {
    // Count a waiter's first entry, including periodic wall-clock rechecks only once.
    thread_local bool counted=false;
    if(!counted) { counted=true; ++entered; }
    return __real_pthread_cond_timedwait(c,m,t);
}
extern "C" int __wrap_pthread_mutex_lock(pthread_mutex_t *m) {
    if(pause_signal && m==target_lock && std::this_thread::get_id()==advancer_id) {
        signal_entered=true;
        while(!release_signal) std::this_thread::sleep_for(100us);
    }
    return __real_pthread_mutex_lock(m);
}
template<typename F> bool until(F f,std::chrono::milliseconds timeout=1500ms) {
    const auto end=std::chrono::steady_clock::now()+timeout;
    while(!f()) { if(std::chrono::steady_clock::now()>end) return false; std::this_thread::sleep_for(100us); }
    return true;
}
struct WaitObject { pthread_mutex_t lock=PTHREAD_MUTEX_INITIALIZER; pthread_cond_t cond=PTHREAD_COND_INITIALIZER; };
int lifecycle() {
    LockstepScheduler ls; ls.set_absolute_time(1);
    WaitObject w; target_lock=&w.lock; pause_signal=true;
    std::atomic<int> result{-1};
    std::thread waiter([&]{pthread_mutex_lock(&w.lock); result=ls.cond_timedwait(&w.cond,&w.lock,100); ++returned; pthread_mutex_unlock(&w.lock);});
    if(!until([]{return entered==1;})) std::_Exit(90);
    std::thread advance([&]{advancer_id=std::this_thread::get_id(); ls.set_absolute_time(100);});
    if(!until([]{return signal_entered.load();})) std::_Exit(91);
    // Force the waiter to wake after timeout collection but before scheduler broadcast.
    pthread_mutex_lock(&w.lock); pthread_cond_broadcast(&w.cond); pthread_mutex_unlock(&w.lock);
    const bool retired_before_broadcast=until([]{return returned==1;},200ms);
    release_signal=true; advance.join();
    if(!until([]{return returned==1;})) std::_Exit(92);
    ls.set_absolute_time(100); waiter.join();
    pthread_cond_destroy(&w.cond); pthread_mutex_destroy(&w.lock);
    std::printf("lifecycle retired_before_scheduler_broadcast=%s result=%d\n",retired_before_broadcast?"true":"false",result.load());
    return retired_before_broadcast?1:0;
}
int fanout() {
    LockstepScheduler ls; ls.set_absolute_time(1);
    constexpr int n=40; WaitObject ws[n]; std::vector<std::thread> threads;
    for(int i=0;i<n;i++) threads.emplace_back([&,i]{auto &w=ws[i]; pthread_mutex_lock(&w.lock); int r=ls.cond_timedwait(&w.cond,&w.lock,100); if(r!=ETIMEDOUT) std::_Exit(93); ++returned; pthread_mutex_unlock(&w.lock);});
    if(!until([]{return entered>=n;})) std::_Exit(94);
    ls.set_absolute_time(100);
    bool all_woke=until([]{return returned==n;},500ms);
    int woke_before_rescue=returned;
    // Rescue legacy stranded waiters so baseline also exits cleanly.
    for(auto &w:ws) {pthread_mutex_lock(&w.lock); pthread_cond_broadcast(&w.cond); pthread_mutex_unlock(&w.lock);}
    if(!until([]{return returned==n;})) std::_Exit(95);
    ls.set_absolute_time(100);
    for(auto &t:threads)t.join();
    for(auto &w:ws) {pthread_cond_destroy(&w.cond); pthread_mutex_destroy(&w.lock);}
    std::printf("fanout woke_without_rescue=%d/%d\n",woke_before_rescue,n);
    return all_woke?0:1;
}
int external_signal() {
    LockstepScheduler ls; ls.set_absolute_time(1);
    WaitObject w; std::atomic<int> result{-1};
    std::thread waiter([&]{pthread_mutex_lock(&w.lock); result=ls.cond_timedwait(&w.cond,&w.lock,100); ++returned; pthread_mutex_unlock(&w.lock);});
    if(!until([]{return entered>=1;})) std::_Exit(96);
    pthread_mutex_lock(&w.lock);
    // A correct notifier may hold the caller mutex beyond the 10ms wall-clock poll.
    std::this_thread::sleep_for(30ms);
    pthread_cond_broadcast(&w.cond);
    pthread_mutex_unlock(&w.lock);
    const bool notified=until([]{return returned==1;},200ms);
    const int result_before_rescue=result;
    // Advance virtual time only for rescue/linked-list cleanup, after observing the failure.
    ls.set_absolute_time(100);
    if(!until([]{return returned==1;})) std::_Exit(97);
    ls.set_absolute_time(100); waiter.join();
    pthread_cond_destroy(&w.cond); pthread_mutex_destroy(&w.lock);
    std::printf("external_signal notified_before_virtual_deadline=%s result_before_rescue=%d final_result=%d\n",notified?"true":"false",result_before_rescue,result.load());
    return notified && result_before_rescue==0?0:1;
}
int main(int argc,char**argv) {if(argc!=2)return 2; if(argv[1][0]=='l')return lifecycle(); if(argv[1][0]=='e')return external_signal(); return fanout();}
