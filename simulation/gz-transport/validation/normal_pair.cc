#include <gz/transport/Node.hh>
#include <gz/msgs/stringmsg.pb.h>
#include <atomic>
#include <chrono>
#include <cstdlib>
#include <iostream>
#include <fstream>
#include <filesystem>
#include <thread>
using namespace std::chrono_literals;
int main(int argc,char **argv)
{
  if(argc!=3)return 2;
  std::string role=argv[1];bool bothStats=std::string(argv[2])=="both-stats";
  gz::transport::Node node;
  std::string prefix=std::getenv("DELTA_PAIR_PREFIX");
  std::string ready=prefix+".subscriber-ready",ack=prefix+".warmup-ack";
  if(role=="publisher")
  {
    auto pub=node.Advertise<gz::msgs::StringMsg>("/delta_official_pair");
    auto end=std::chrono::steady_clock::now()+1800ms;
    while(!pub.HasConnections()&&std::chrono::steady_clock::now()<end)std::this_thread::sleep_for(5ms);
    bool connected=pub.HasConnections();bool ok=connected;
    bool acknowledged=false;int warmupSent=0;
    auto ackEnd=std::chrono::steady_clock::now()+1400ms;
    while(connected&&!std::filesystem::exists(ack)&&std::chrono::steady_clock::now()<ackEnd)
    {gz::msgs::StringMsg msg;msg.set_data("warmup");pub.Publish(msg);++warmupSent;std::this_thread::sleep_for(100ms);}
    acknowledged=std::filesystem::exists(ack);ok=ok&&acknowledged;
    for(int i=0;i<3&&acknowledged;++i)
    {gz::msgs::StringMsg msg;msg.set_data("normal-"+std::to_string(i));ok=pub.Publish(msg)&&ok;std::this_thread::sleep_for(30ms);}
    std::cout<<"{\"role\":\"publisher\",\"connected\":"<<connected<<",\"warmups_sent\":"<<warmupSent<<",\"application_warmup_ack\":"<<acknowledged<<",\"three_publishes_ok\":"<<ok<<"}"<<std::endl;
    std::_Exit(ok?0:8);
  }
  std::atomic<int> count{0},wrong{0},warmups{0};std::atomic<bool> enabled{false};
  std::function<void(const gz::msgs::StringMsg &)> cb=[&](const gz::msgs::StringMsg &msg)
  {if(msg.data()=="warmup")
   {if(warmups.fetch_add(1)==0){enabled=node.EnableStats("/delta_official_pair",true,"/delta_official_statistics",1000);std::ofstream(ack)<<"typed warmup received; statistics registration complete\n";}return;}
   int n=count.fetch_add(1);if(msg.data()!="normal-"+std::to_string(n))++wrong;};
  bool subscribed=node.Subscribe<gz::msgs::StringMsg>("/delta_official_pair",cb);
  std::ofstream(ready)<<"Subscribe returned before peer launch\n";
  auto end=std::chrono::steady_clock::now()+3600ms;
  while(count.load()<3&&std::chrono::steady_clock::now()<end)std::this_thread::sleep_for(5ms);
  auto stats=node.TopicStats("/delta_official_pair");
  bool statsPresent=stats.has_value();uint64_t dropped=stats?stats->DroppedMsgCount():0;
  uint64_t periods=stats?stats->PublicationStatistics().Count():0;
  bool ok=subscribed&&enabled&&count==3&&wrong==0&&(statsPresent==bothStats)&&(!bothStats||(dropped==0&&periods==static_cast<uint64_t>(warmups.load()+1)));
  std::cout<<"{\"role\":\"subscriber\",\"subscribed\":"<<subscribed<<",\"warmups_received\":"<<warmups<<",\"received\":"<<count<<",\"wrong_payload_order\":"<<wrong
    <<",\"statistics_present\":"<<statsPresent<<",\"dropped\":"<<dropped<<",\"period_samples\":"<<periods<<",\"acceptance\":"<<ok<<"}"<<std::endl;
  std::_Exit(ok?0:17); // bounded legacy failure, no pretending deadlocked cleanup passed
}
