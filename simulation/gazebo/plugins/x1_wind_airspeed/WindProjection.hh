#pragma once

namespace multidetect::x1
{
// Ground speed is already along the pitot's +X axis. Wind and the unit
// forward axis are expressed in the same Gazebo world frame.
template <class Vector>
inline double AxialAirspeed(double groundForward, const Vector &windWorld,
                            const Vector &forwardWorld)
{
  const double windForward = windWorld.Dot(forwardWorld);
  return windForward == 0.0 ? groundForward : groundForward - windForward;
}
}
