import { Pause, Play, Volume2 } from "lucide-react";
import { TouchIconButton } from "@/components/common/TouchIconButton";
import type { AudioPlayer } from "@/hooks/useAudioPlayer";

type Props = {
  player: AudioPlayer;
};

export function TouchJukebox({ player }: Props) {
  return (
    <div className="touch-ui pointer-events-auto absolute bottom-4 right-4 z-30 items-center gap-2">
      <TouchIconButton
        icon={
          player.playing === true ? (
            <Pause size={20} fill="currentColor" aria-hidden="true" />
          ) : (
            <Play size={20} fill="currentColor" aria-hidden="true" />
          )
        }
        onClick={player.togglePlayback}
        aria-label={player.playing === true ? "Wstrzymaj muzykę" : "Odtwórz muzykę"}
      />
      <label className="flex h-12 w-32 items-center gap-2 rounded-full border border-white/20 bg-panel/65 px-3 text-white/80 shadow-lg backdrop-blur-sm">
        <Volume2 size={18} className="shrink-0" aria-hidden="true" />
        <input
          type="range"
          min="0"
          max="1"
          step="0.05"
          value={player.volume}
          onChange={(event) => player.setVolume(Number(event.target.value))}
          className="min-w-0 flex-1 cursor-pointer accent-primary"
          aria-label="Głośność muzyki"
        />
      </label>
    </div>
  );
}
