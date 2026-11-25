#include "WindProjection.hh"

#include <memory>
#include <unordered_map>
#include <vector>

#include <gz/common/Console.hh>
#include <gz/plugin/Register.hh>
#include <gz/sensors/AirSpeedSensor.hh>
#include <gz/sensors/SensorFactory.hh>
#include <gz/sim/EntityComponentManager.hh>
#include <gz/sim/System.hh>
#include <gz/sim/Util.hh>
#include <gz/sim/components/AirSpeedSensor.hh>
#include <gz/sim/components/LinearVelocity.hh>
#include <gz/sim/components/Name.hh>
#include <gz/sim/components/ParentEntity.hh>
#include <gz/sim/components/Pose.hh>
#include <gz/sim/components/Sensor.hh>
#include <gz/sim/components/Wind.hh>

namespace multidetect::x1
{
namespace sim = gz::sim;
namespace components = gz::sim::components;

class WindAirspeed final : public sim::System,
                           public sim::ISystemPreUpdate,
                           public sim::ISystemPostUpdate
{
  public: void PreUpdate(const sim::UpdateInfo &,
                         sim::EntityComponentManager &ecm) override
  {
    for (const auto entity : newTopics)
    {
      const auto sensor = sensors.find(entity);
      if (sensor != sensors.end() && ecm.HasEntity(entity))
        ecm.CreateComponent(entity, components::SensorTopic(sensor->second->Topic()));
    }
    newTopics.clear();
  }

  public: void PostUpdate(const sim::UpdateInfo &info,
                          const sim::EntityComponentManager &ecm) override
  {
    const auto add = [this, &ecm](const sim::Entity &entity,
                                 const components::AirSpeedSensor *component,
                                 const components::ParentEntity *parent)
    {
      auto config = component->Data();
      config.SetName(sim::removeParentScope(sim::scopedName(entity, ecm, "::", false), "::"));
      if (config.Topic().empty())
        config.SetTopic(sim::scopedName(entity, ecm) + "/air_speed");

      auto sensor = gz::sensors::SensorFactory().CreateSensor<gz::sensors::AirSpeedSensor>(config);
      if (!sensor)
      {
        gzerr << "X1 wind airspeed failed to load [" << config.Name() << "]\n";
        return true;
      }
      const auto *parentName = ecm.Component<components::Name>(parent->Data());
      if (parentName)
        sensor->SetParent(parentName->Data());
      sensor->SetPose(sim::worldPose(entity, ecm));
      newTopics.push_back(entity);
      sensors.emplace(entity, std::move(sensor));
      return true;
    };

    if (!initialized)
    {
      ecm.Each<components::AirSpeedSensor, components::ParentEntity>(add);
      initialized = true;
    }
    else
      ecm.EachNew<components::AirSpeedSensor, components::ParentEntity>(add);

    ecm.EachRemoved<components::AirSpeedSensor>(
        [this](const sim::Entity &entity, const components::AirSpeedSensor *)
        {
          sensors.erase(entity);
          return true;
        });

    if (info.paused || sensors.empty())
      return;

    const auto windEntity = ecm.EntityByComponents(components::Wind());
    const auto *wind = ecm.Component<components::WorldLinearVelocity>(windEntity);
    for (auto &[entity, sensor] : sensors)
    {
      if (sensor->NextDataUpdateTime() > info.simTime || !sensor->HasConnections())
        continue;
      if (const auto *pose = ecm.Component<components::WorldPose>(entity))
      {
        auto velocity = sim::relativeVel(entity, ecm);
        if (wind)
        {
          // Use the same pose hierarchy as relativeVel for the frame change.
          const auto forward = sim::worldPose(entity, ecm).Rot().RotateVector(gz::math::Vector3d::UnitX);
          velocity.X(AxialAirspeed(velocity.X(), wind->Data(), forward));
        }
        // Keep the stock pose input for the differential pressure model.
        sensor->SetPose(pose->Data());
        sensor->SetVelocity(velocity);
      }
      sensor->Update(info.simTime, false);
    }
  }

  private: bool initialized{false};
  private: std::vector<sim::Entity> newTopics;
  private: std::unordered_map<sim::Entity, std::unique_ptr<gz::sensors::AirSpeedSensor>> sensors;
};
}

GZ_ADD_PLUGIN(multidetect::x1::WindAirspeed, gz::sim::System,
              gz::sim::ISystemPreUpdate, gz::sim::ISystemPostUpdate)
