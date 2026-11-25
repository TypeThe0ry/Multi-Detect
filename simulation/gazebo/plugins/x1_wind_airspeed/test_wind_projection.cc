#include "WindProjection.hh"

#include <cmath>
#include <iostream>

namespace
{
struct Vec3
{
  double x, y, z;
  double Dot(const Vec3 &other) const
  {
    return x * other.x + y * other.y + z * other.z;
  }
};
}

int main()
{
  using multidetect::x1::AxialAirspeed;
  const Vec3 east{1, 0, 0};
  const Vec3 north{0, 1, 0};
  const Vec3 up{0, 0, 1};
  const double diagonal = std::sqrt(0.5);
  const struct Case {const char *label; double actual; double expected;} cases[] = {
    {"zero", AxialAirspeed(17.125, Vec3{0, 0, 0}, east), 17.125},
    {"headwind", AxialAirspeed(16, Vec3{-8, 0, 0}, east), 24},
    {"tailwind", AxialAirspeed(16, Vec3{8, 0, 0}, east), 8},
    {"crosswind", AxialAirspeed(16, Vec3{0, 8, 0}, east), 16},
    {"yaw", AxialAirspeed(16, Vec3{0, -8, 3}, north), 24},
    {"vertical", AxialAirspeed(0, Vec3{1, 2, -3}, up), 3},
    {"tilted", AxialAirspeed(16, Vec3{3, 4, -3}, Vec3{diagonal, 0, diagonal}), 16},
    {"reverse flow", AxialAirspeed(2, Vec3{8, 0, 0}, east), -6},
  };
  for (const auto &entry : cases)
  {
    if (!(std::abs(entry.actual - entry.expected) < 1e-12))
    {
      std::cerr << entry.label << ": expected " << entry.expected << ", got " << entry.actual << '\n';
      return 1;
    }
  }
}
