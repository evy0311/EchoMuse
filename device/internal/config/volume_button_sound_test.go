package config

import "testing"

func TestVolumeButtonSoundCanBeTurnedOff(t *testing.T) {
	d := &Device{}
	on := true
	d.Apply(ConfigMessage{VolumeButtonSound: &on})
	if !d.VolumeButtonSoundEnabled() {
		t.Fatal("volume button sound did not turn on")
	}

	off := false
	d.Apply(ConfigMessage{VolumeButtonSound: &off})
	if d.VolumeButtonSoundEnabled() {
		t.Fatal("volume button sound did not turn off — false was treated as absent")
	}
}
