import * as THREE from "three";

const ANIMATION_EPSILON = 1e-4;

function nearlyEqual(a: number, b: number) {
  return Math.abs(a - b) <= ANIMATION_EPSILON * Math.max(1, Math.abs(a), Math.abs(b));
}

function trackFramesEqual(track: THREE.KeyframeTrack, a: number, b: number) {
  const valueSize = track.getValueSize();
  for (let component = 0; component < valueSize; component += 1) {
    if (
      nearlyEqual(
        track.values[a * valueSize + component],
        track.values[b * valueSize + component],
      ) === false
    )
      return false;
  }
  return true;
}

function isConstantTrack(track: THREE.KeyframeTrack) {
  for (let frame = 1; frame < track.times.length; frame += 1) {
    if (trackFramesEqual(track, 0, frame) === false) return false;
  }
  return true;
}

function findTrackPeriod(track: THREE.KeyframeTrack) {
  const frameCount = track.times.length;
  for (let period = 1; period < frameCount - 1; period += 1) {
    // Okres może się zaczynać tylko od klatki równej pierwszej. Ten szybki
    // test eliminuje kosztowne porównywanie całego ogona dla większości klatek.
    if (trackFramesEqual(track, 0, period) === false) continue;

    let repeats = true;
    for (let frame = period; frame < frameCount; frame += 1) {
      if (trackFramesEqual(track, frame, frame % period) === false) {
        repeats = false;
        break;
      }
    }
    if (repeats === true) return period;
  }
  return frameCount - 1;
}

export function createIndependentLoopClips(sourceClips: THREE.AnimationClip[]) {
  const clips: THREE.AnimationClip[] = [];

  sourceClips.forEach((sourceClip) => {
    sourceClip.tracks.forEach((sourceTrack, index) => {
      if (sourceTrack.times.length < 2 || isConstantTrack(sourceTrack) === true) return;

      const track = sourceTrack.clone();
      const frameCount = findTrackPeriod(sourceTrack) + 1;
      track.times = track.times.slice(0, frameCount);
      track.values = track.values.slice(0, frameCount * track.getValueSize());

      const startTime = track.times[0];
      if (startTime !== 0) {
        for (let frame = 0; frame < track.times.length; frame += 1) track.times[frame] -= startTime;
      }

      const duration = track.times[track.times.length - 1];
      clips.push(
        new THREE.AnimationClip(`${sourceClip.name}:${track.name}:${index}`, duration, [track]),
      );
    });
  });

  return clips;
}
