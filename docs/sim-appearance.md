# Gazebo Harmonic SIM appearance

The generated SDF uses the fields that the Harmonic `ogre2` scene loader
consumes directly:

* `<scene><ambient>`, `<background>`, and `<shadows>` are scene properties;
* a directional `<light>` accepts `<direction>`, `<diffuse>`, `<specular>`,
  `<intensity>`, and `<cast_shadows>`;
* a light's direction vector is a world-space vector whose negative Z component
  places the sun above the ground.  The low-sun profiles use elevations of roughly 13 and 17 degrees; dusk uses roughly 5 degrees.

These are also the exact elements emitted by the existing repository worlds and
are parsed by the CPU SDF tests.  The reference SDF definitions are the Gazebo
Harmonic SDF 1.9 specification and the `gz-sim` rendering/light tutorials:

* <https://sdformat.org/spec/1.9.html#world_scene>
* <https://sdformat.org/spec/1.9.html#light>
* <https://gazebosim.org/docs/harmonic/rendering/>

`<scene><fog>` is valid SDF syntax, but the Harmonic Ogre2 path does not provide
a portable, deterministic world-fog render contract for this camera setup.  We
therefore do not emit it.  The `haze` preset lowers contrast and raises the
ambient/background values; image-space `--image-augmentation weather` adds
seeded brightness, contrast, colour-temperature, blur, and sensor-noise shifts
to the slow-model SFT builder.  If a deployment camera variant later exposes
Ogre2 exposure/noise controls, those can be added to that variant model without
touching the frozen `x500_mono_cam/model.sdf`.
