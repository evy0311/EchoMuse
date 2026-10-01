// Package cue synthesises the device's own audible cues.
//
// The wake cue (#120) is the first sound this device has ever made of its own
// accord — everything else it plays came off the wire. It exists because the
// LED ring is the ONLY indication that the device is listening, and a ring is
// no use from the next room (@tvories' original report) and no use at all to a
// blind or low-vision user, which is the framing that makes it an
// accessibility setting rather than a nicety.
//
// GENERATED, NOT SHIPPED AS AN ASSET. A synthesised tone needs no distribution
// path, no md5 dance, no OTA payload and no second copy to keep in step with
// the firmware — compare `em_oww_assets`, which exists entirely to get 15MB of
// wake-word runtime onto a device. Two sine bursts cost a few hundred bytes of
// code and are exact on every device.
//
// Pure Go, no build tag, no hardware: this is arithmetic and the whole point is
// that it is testable on the host. The speaker binding mixes what it returns.
package cue

import "math"

// The wake cue: a rising two-tone. Lower note, then higher — "uh huh, I'm
// listening" (Wil, 2026-08-22).
//
// A RISING interval is the whole design. Falling reads as dismissal or error
// ("nope"), level reads as a notification; rising is the questioning,
// attentive shape, and it is what every assistant that has tested this on
// people converged on. 660Hz to 880Hz is a perfect fourth, the most
// unambiguous rise available in a short span.
//
// The pitches also sit where this hardware is good. The bass guard removes
// everything below 115Hz because the driver cannot deliver it, and stock's own
// correction boosts 150–250Hz by ~25dB (#247) — a small speaker's useful range
// is the midrange, and 660/880Hz is squarely in it. A cue pitched low would be
// the one thing on this device guaranteed to be inaudible across a room.
const (
	BingHz = 660.0 // E5
	BongHz = 880.0 // A5 — a perfect fourth above

	bingMS = 90.0
	bongMS = 130.0
	// A short gap makes it two notes rather than a glissando. Without it the
	// phase discontinuity at the pitch change is also audible as a tick.
	gapMS = 25.0

	// Raised-cosine edges. A sine burst that starts or stops at full
	// amplitude is a step, and a step is a click — the same discontinuity
	// the output chain's crossfade exists to avoid, arrived at from the
	// other direction. 4ms is inaudible as a fade and completely removes it.
	edgeMS = 4.0

	// The volume cue is a low, struck chime rather than the wake cue's bright
	// rising interval: it confirms a level change without saying "I'm
	// listening". The pitch starts a little sharp and settles quickly onto A3,
	// giving it the short "duhn" shape of a damped bell instead of a high
	// electronic beep. 220Hz also sits in the range stock's speaker correction
	// supports strongly on this small driver (#247).
	//
	// Its peak is scaled by the device volume passed to VolumeCue, so it is
	// useful as an audible preview of that level rather than a fixed alert.
	volumeHz        = 220.0 // A3
	volumeStartRise = 0.28  // begin 28% sharp, then settle onto volumeHz
	volumeMS        = 145.0
	// The cue bypasses EQ/output processing so its timbre is invariant, but
	// follows the same VolumeGain curve as playback. -2dBFS is a 4dB lift over
	// the first audition — exactly one physical-button step — to put the short,
	// low-decay sound closer to the perceived level of ordinary programme audio.
	volumePeakDB = -2.0
)

// The wake sound's three levels, as set by `wakeSoundLevel`.
const (
	LevelQuiet  = "quiet"
	LevelMedium = "medium"
	LevelLoud   = "loud"
)

// Levels lists them quietest first.
var Levels = []string{LevelQuiet, LevelMedium, LevelLoud}

// LevelDBFS is a level's peak in dBFS; anything unrecognised is medium.
//
// ABSOLUTE levels: the cue is mixed after the software volume, with the DAC
// at unity, so these are what reaches the speaker whatever the volume. For
// scale, the first cue peaked at -10dBFS before a typical volume of 85
// (-21dB), about -31dBFS here, and was heard as understated (Wil,
// 2026-09-24). Starting points, 10dB apart, to be set by ear.
func LevelDBFS(level string) float64 {
	switch level {
	case LevelQuiet:
		return -30
	case LevelLoud:
		return -10
	}
	return -20
}

// WakeCue renders the cue at the given sample rate and peak level, in S16
// units (±32768) to match everything else on the speaker path.
func WakeCue(sampleRate int, peakDBFS float64) []float64 {
	fs := float64(sampleRate)
	amp := math.Pow(10, peakDBFS/20.0) * 32768.0

	bing := note(fs, BingHz, bingMS, amp)
	gap := int(fs * gapMS / 1000.0)
	bong := note(fs, BongHz, bongMS, amp)

	out := make([]float64, 0, len(bing)+gap+len(bong))
	out = append(out, bing...)
	out = append(out, make([]float64, gap)...)
	out = append(out, bong...)
	return out
}

// VolumeCue renders the physical-volume-button confirmation tone. volumeGain
// is the linear gain for the NEW device volume (speaker.VolumeGain at the call
// site). The cue mixer sits after software volume, so the gain is baked into
// these samples exactly once.
func VolumeCue(sampleRate int, volumeGain float64) []float64 {
	if volumeGain <= 0 {
		return nil
	}
	if volumeGain > 1 {
		volumeGain = 1
	}
	peak := math.Pow(10, volumePeakDB/20.0) * 32768.0 * volumeGain
	return struckChime(float64(sampleRate), volumeHz, volumeMS, peak)
}

// struckChime renders the low, damped volume-button "duhn". Its pitch bend is
// continuous (phase is accumulated sample by sample), so settling the note
// cannot introduce the click a two-burst implementation would. A brief second
// harmonic supplies the attack that lets a 220Hz fundamental read on this
// speaker, then gets out of the way faster than the body of the note.
func struckChime(fs, freq, ms, amp float64) []float64 {
	n := int(fs * ms / 1000.0)
	if n <= 0 {
		return nil
	}
	edge := int(fs * edgeMS / 1000.0)
	if edge*2 > n {
		edge = n / 2
	}

	out := make([]float64, n)
	phase := 0.0
	for i := 0; i < n; i++ {
		x := float64(i) / float64(n)
		// The sharp attack settles during the first half of the note. Keeping
		// the tail close to A3 is what makes it sound struck rather than swept.
		instantHz := freq * (1 + volumeStartRise*math.Exp(-7*x))
		phase += 2 * math.Pi * instantHz / fs

		body := math.Sin(phase)
		attack := 0.24 * math.Sin(2*phase) * math.Exp(-8*x)
		decay := math.Exp(-4.8 * x)

		// Raised-cosine edges always win over the decay, guaranteeing silence
		// at both ends and no button-click discontinuity.
		env := 1.0
		if i < edge {
			env = 0.5 * (1 - math.Cos(math.Pi*float64(i)/float64(edge)))
		} else if i >= n-edge {
			k := float64(n - 1 - i)
			env = 0.5 * (1 - math.Cos(math.Pi*k/float64(edge)))
		}

		out[i] = amp * (body + attack) * decay * env / 1.24
	}
	return out
}

// note renders one tone with raised-cosine edges and a gentle decay.
//
// The decay is what makes it a "bing" rather than a "beep": a flat-topped
// burst reads as a machine alert, a decaying one reads as something struck.
// It is shallow — this has to stay audible for its whole length across a room.
func note(fs, freq, ms, amp float64) []float64 {
	n := int(fs * ms / 1000.0)
	if n <= 0 {
		return nil
	}
	edge := int(fs * edgeMS / 1000.0)
	if edge*2 > n {
		edge = n / 2
	}

	out := make([]float64, n)
	w := 2 * math.Pi * freq / fs
	for i := 0; i < n; i++ {
		t := float64(i)
		// A touch of second harmonic gives the note some body on a small
		// driver, where a pure sine can read as thin and synthetic.
		s := math.Sin(w*t) + 0.12*math.Sin(2*w*t)

		// Shallow exponential decay across the note.
		decay := math.Exp(-2.2 * t / float64(n))

		// Raised-cosine edges, applied last so they always win: whatever the
		// decay is doing, the first and last samples are zero.
		env := 1.0
		if i < edge {
			env = 0.5 * (1 - math.Cos(math.Pi*float64(i)/float64(edge)))
		} else if i >= n-edge {
			k := float64(n - 1 - i)
			env = 0.5 * (1 - math.Cos(math.Pi*k/float64(edge)))
		}

		out[i] = amp * s * decay * env / 1.12 // normalise for the harmonic
	}
	return out
}

// DurationMS is how long the whole cue lasts — needed by the mic path, which
// has to know which frames to exclude from the wake scorer and the ASR stream.
// The device is the one making the sound, so it can say exactly; that is the
// main reason the cue is generated here rather than sent by the controller.
func DurationMS() float64 { return bingMS + gapMS + bongMS }
