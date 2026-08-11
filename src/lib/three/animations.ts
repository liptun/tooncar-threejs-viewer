import * as THREE from "three";

const ANIMATION_EPSILON = 1e-4;

function nearlyEqual(a: number, b: number) {
  return Math.abs(a - b) <= ANIMATION_EPSILON * Math.max(1, Math.abs(a), Math.abs(b));
}

function animationError(track: THREE.KeyframeTrack, message: string) {
  return new Error(`Nieprawidłowy kanał animacji "${track.name}": ${message}`);
}

function validateSourceTrack(track: THREE.KeyframeTrack) {
  const frameCount = track.times.length;
  const valueSize = track.getValueSize();

  if (frameCount === 0) throw animationError(track, "brak klatek kluczowych.");
  if (Number.isInteger(valueSize) === false || valueSize <= 0)
    throw animationError(track, "liczba wartości nie pasuje do liczby klatek.");
  if (track.values.length !== frameCount * valueSize)
    throw animationError(track, "tablice czasu i wartości mają niespójne długości.");

  for (let frame = 0; frame < frameCount; frame += 1) {
    const time = track.times[frame];
    if (Number.isFinite(time) === false)
      throw animationError(track, `klatka ${frame} ma nieprawidłowy czas.`);
    if (frame > 0 && time <= track.times[frame - 1])
      throw animationError(track, "czasy klatek nie są rosnące.");
  }

  for (let index = 0; index < track.values.length; index += 1) {
    if (Number.isFinite(track.values[index]) === false)
      throw animationError(track, `wartość ${index} nie jest liczbą skończoną.`);
  }

  return valueSize;
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

function createLoopTrack(sourceTrack: THREE.KeyframeTrack, valueSize: number) {
  const frameCount = findTrackPeriod(sourceTrack) + 1;
  const valueCount = frameCount * valueSize;
  const startTime = sourceTrack.times[0];
  const duration = sourceTrack.times[frameCount - 1] - startTime;

  if (Number.isFinite(duration) === false || duration <= ANIMATION_EPSILON)
    throw animationError(sourceTrack, "czas trwania pętli musi być większy od zera.");

  const times = sourceTrack.times.slice(0, frameCount);
  const values = sourceTrack.values.slice(0, valueCount);
  for (let frame = 0; frame < times.length; frame += 1) times[frame] -= startTime;

  const track = sourceTrack.clone();
  track.times = times;
  track.values = values;

  if (track.getValueSize() !== valueSize)
    throw animationError(sourceTrack, "przycięcie zmieniło liczbę komponentów klatki.");

  return { track, duration };
}

export function createIndependentLoopClips(sourceClips: THREE.AnimationClip[]) {
  const clips: THREE.AnimationClip[] = [];

  sourceClips.forEach((sourceClip) => {
    sourceClip.tracks.forEach((sourceTrack, index) => {
      const valueSize = validateSourceTrack(sourceTrack);
      if (sourceTrack.times.length < 2 || isConstantTrack(sourceTrack) === true) return;

      const { track, duration } = createLoopTrack(sourceTrack, valueSize);
      clips.push(
        new THREE.AnimationClip(`${sourceClip.name}:${track.name}:${index}`, duration, [track]),
      );
    });
  });

  return clips;
}
