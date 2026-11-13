#include <gz/transport/Node.hh>
#include <gz/transport/NodeShared.hh>
#include <gz/transport/TopicUtils.hh>
#include <zmq.h>
#include <atomic>
#include <cstring>
#include <chrono>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#include <sys/syscall.h>
#include <unistd.h>
using namespace std::chrono_literals;
int Owner(gz::transport::NodeShared *s)
{ return __atomic_load_n(&s->mutex.native_handle()->__data.__owner, __ATOMIC_RELAXED); }
template<class F> bool Until(F f, int ms)
{
  auto end=std::chrono::steady_clock::now()+std::chrono::milliseconds(ms);
  do { if(f())return true;std::this_thread::sleep_for(2ms); } while(std::chrono::steady_clock::now()<end);
  return f();
}
void Send(void *sock, const std::vector<std::string> &v, bool leaveOpen=false)
{
  for(size_t i=0;i<v.size();++i)
    if(zmq_send(sock,v[i].data(),v[i].size(),(i+1<v.size()||leaveOpen)?ZMQ_SNDMORE:0)<0)
      throw std::runtime_error("own XPUB send failed");
}
int main(int argc,char **argv)
{
  if(argc!=3)return 2;
  bool legacy=std::string(argv[2])=="legacy";
  std::string mode=argv[1];
  if(mode!="valid-four"&&mode!="complete-one"&&mode!="uncommitted-multipart"&&mode!="extra-fifth"&&mode!="valid-five"&&mode!="six"&&mode!="three"&&mode!="fault-eagain"&&mode!="fault-eintr"&&mode!="metadata-content")return 2;
  try
  {
    gz::transport::Node node;
    auto *shared=gz::transport::NodeShared::Instance();
    std::atomic<int> statsUpdates{0};std::atomic<uint64_t> statsDropped{0};std::atomic<double> statsPeriod{0};
    const std::string topic="/delta_recv_probe";
    std::string full;
    if(!gz::transport::TopicUtils::FullyQualifiedName(node.Options().Partition(),"",topic,full))return 3;
    std::atomic<int> callbacks{0},wrongPayload{0};
    gz::transport::RawCallback cb=[&](const char *b,size_t n,const gz::transport::MessageInfo &){if(std::string(b,n)!="opaque component raw bytes")++wrongPayload;++callbacks;};
    if(!node.SubscribeRaw(topic,cb))return 3;
    if(mode=="metadata-content")shared->EnableStats(full,true,[&](const gz::transport::TopicStatistics &v){statsDropped=v.DroppedMsgCount();statsPeriod=v.PublicationStatistics().Avg();++statsUpdates;});
    const std::string data="opaque component raw bytes",type="delta.component.raw";
    uint64_t metaWords[2]={1000,10};
    const std::string validMeta(reinterpret_cast<const char *>(metaWords),sizeof metaWords);
    auto victim=node.Advertise("/delta_recv_victim",type);
    void *ctx=zmq_ctx_new(),*xpub=zmq_socket(ctx,ZMQ_XPUB);
    int zero=0,one=1,timeout=1000;
    zmq_setsockopt(xpub,ZMQ_LINGER,&zero,sizeof zero);
    zmq_setsockopt(xpub,ZMQ_SNDTIMEO,&timeout,sizeof timeout);
    zmq_setsockopt(xpub,ZMQ_XPUB_VERBOSE,&one,sizeof one);
    if(zmq_bind(xpub,"tcp://127.0.0.1:*")!=0)return 3;
    char endpoint[256];size_t endpointSize=sizeof endpoint;
    zmq_getsockopt(xpub,ZMQ_LAST_ENDPOINT,endpoint,&endpointSize);
    // Explicit public-but-internal connection-callback injection. Real Node
    // subscriber socket, receiver thread, S and library parsing are untouched.
    gz::transport::MessagePublisher injected(full,endpoint,"unused",
      "delta_foreign_process_"+std::to_string(getpid()),"delta_pub_node",type,
      gz::transport::AdvertiseMessageOptions());
    shared->OnNewConnection(injected);
    bool subscribed=false;
    Until([&]
    {
      zmq_pollitem_t item{xpub,0,ZMQ_POLLIN,0};
      if(zmq_poll(&item,1,0)>0)
      {
        char b[8192];int n=zmq_recv(xpub,b,sizeof b,ZMQ_DONTWAIT);
        if(n>1&&b[0]==1&&std::string(b+1,n-1)==full)subscribed=true;
      }
      return subscribed;
    },1500);
    if(!subscribed)throw std::runtime_error("exact XPUB subscription not observed");
    bool fault=mode=="fault-eagain"||mode=="fault-eintr";
    bool multipart=mode=="uncommitted-multipart";
    bool five=mode=="extra-fifth"||mode=="valid-five"||mode=="metadata-content";
    bool initialGood=mode=="valid-four"||mode=="valid-five"||mode=="metadata-content";
    bool legacyBlocked=legacy&&mode!="valid-four"&&mode!="metadata-content"&&!multipart;
    if(mode=="valid-four"||fault)Send(xpub,{full,"sender",data,type});
    else if(mode=="complete-one")Send(xpub,{full});
    else if(mode=="three")Send(xpub,{full,"sender",data});
    else if(mode=="valid-five"||mode=="metadata-content")Send(xpub,{full,"sender",data,type,validMeta});
    else if(mode=="extra-fifth")Send(xpub,{full,"sender",data,type,"orphan-metadata"});
    else if(mode=="six")Send(xpub,{full,"sender",data,type,validMeta,"extra-sixth"});
    else Send(xpub,{full},true);
    bool callbackBefore=false,sBusy=false;
    int stableOwner=0;
    int initialExpected=legacy?(five||mode=="six"||mode=="metadata-content"?1:mode=="valid-four"?1:0):(initialGood?1:0);
    if(initialExpected)callbackBefore=Until([&]{return callbacks.load()==1;},700);
    if(legacyBlocked)
    {
      sBusy=Until([&]
      {
        if(shared->mutex.try_lock()){shared->mutex.unlock();return false;}
        stableOwner=Owner(shared);return stableOwner>0;
      },700);
    }
    else std::this_thread::sleep_for(80ms);
    std::atomic<bool> published{false};std::atomic<int> victimTid{0};
    std::thread publishThread([&]
    {
      victimTid=static_cast<int>(syscall(SYS_gettid));
      victim.PublishRaw(data,type);published=true;
    });
    bool publishBefore=Until([&]{return published.load();},200);
    int ownerAfter200=Owner(shared);
    bool callbackAbsentBefore=callbacks.load()==0;
    if(legacyBlocked)
    {
      if(mode=="three"||fault)Send(xpub,{full});
      else if(mode=="six")Send(xpub,{full,type});
      else Send(xpub,{full,data,type});
    }
    else if(multipart)Send(xpub,{"sender",data,type});
    int expectedAfter=legacy?(fault?0:1):(initialGood||multipart?1:0);
    bool gotCallback=Until([&]{return callbacks.load()==expectedAfter;},1000);
    bool recovered=Until([&]{return published.load();},1000);
    bool ownerIsOther=stableOwner>0&&stableOwner!=static_cast<int>(syscall(SYS_gettid))&&stableOwner!=victimTid.load();
    bool pass=subscribed&&gotCallback&&recovered;
    if(legacyBlocked)pass=pass&&sBusy&&stableOwner==ownerAfter200&&ownerIsOther&&!publishBefore;
    else pass=pass&&publishBefore;
    int beforeFollow=callbacks.load();
    if(mode=="metadata-content"){uint64_t next[2]={1010,12};Send(xpub,{full,"sender",data,type,std::string(reinterpret_cast<const char *>(next),sizeof next)});}
    else Send(xpub,{full,"sender",data,type});
    bool followup=Until([&]{return callbacks.load()==beforeFollow+1;},1000);
    pass=pass&&followup&&wrongPayload.load()==0;
    if(mode=="metadata-content")pass=pass&&statsUpdates==2&&statsDropped==1&&statsPeriod==10.0;
    std::cout<<"{\"case\":\""<<mode<<"\",\"exact_subscription_seen\":"<<subscribed
      <<",\"callback_before_action\":"<<callbackBefore<<",\"S_busy_observed\":"<<sBusy
      <<",\"S_owner_before_victim\":"<<stableOwner<<",\"S_owner_after_200ms\":"<<ownerAfter200
      <<",\"victim_tid\":"<<victimTid<<",\"owner_is_other_thread\":"<<ownerIsOther
      <<",\"publish_returned_before_action\":"<<publishBefore
      <<",\"callback_absent_before_action\":"<<callbackAbsentBefore
      <<",\"callback_after_action\":"<<gotCallback<<",\"publish_after_action\":"<<recovered
      <<",\"metadata_size\":"<<validMeta.size()<<",\"statistics_updates\":"<<statsUpdates<<",\"statistics_dropped\":"<<statsDropped<<",\"statistics_period_ms\":"<<statsPeriod<<",\"following_normal_message_delivered\":"<<followup<<",\"wrong_payloads\":"<<wrongPayload<<",\"expected_mechanism_observation_pass\":"<<pass<<"}"<<std::endl;
    if(!recovered)std::_Exit(6); // outer 7s process deadline is an additional bound
    publishThread.join();
    node.Unsubscribe(topic);zmq_close(xpub);zmq_ctx_term(ctx);
    return pass?0:4;
  }
  catch(const std::exception &e){std::cerr<<e.what()<<std::endl;return 5;}
}
