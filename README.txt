RotaryController - Rotary Photo Selection

Hardware:
- Raspberry Pi 4
- Autonics E50S8-360-3-T-1
- Encoder A -> GPIO17 (physical pin 11) through the required 5V-to-3.3V signal interface
- Encoder B -> GPIO27 (physical pin 13) through the required 5V-to-3.3V signal interface
- Encoder 0V -> Raspberry Pi GND (physical pin 6)
- Encoder Z is unused
- Encoder +V must use the correct encoder supply; do not connect an output wire to a Pi GPIO.

Photo behavior:
- Photo 1..Photo 33 are the normal sequence.
- home.jpeg is separate and is never part of the sequence.
- E50S8-360-3-T-1 is treated as 360 A pulses/revolution.
- One A rising edge = one degree.
- 45 encoder pulses = exactly one photo change.
- Clockwise: +1 photo; anticlockwise: -1 photo.
- Photo 33 wraps to Photo 1 clockwise.
- Photo 1 wraps to Photo 33 anticlockwise.
- After 60 seconds with no encoder input, home.jpeg fades in.
- The next 45-degree encoder movement leaves Home and selects the next/previous normal photo.
- No automatic slideshow is used.
- The physical push button does not start a slideshow.

Image files:
- 1..33 may be JPG/JPEG/PNG/BMP/HEIC/HEIF.
- home may be home.jpeg/home.jpg/home.png/home.bmp/home.heic/home.heif.
- The supplied .heic files are JPEG image data with a .heic extension; the loader handles them.

If clockwise/anticlockwise is reversed:
- Set "clockwise_when_b_low" in config.json from true to false.

Do not change the GPIO pins unless the wiring is changed accordingly.
