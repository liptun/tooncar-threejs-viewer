import { Image, RotateCcw, Video } from "lucide-react";
import { TouchIconButton } from "@/components/common/TouchIconButton";
import { RtxIcon } from "@/components/common/RtxIcon";
import type { CameraLinkType } from "@/hooks/useTrackViewer";
import { cn } from "@/lib/cn";

type Props = {
  copiedCameraLink: CameraLinkType | null;
  enhancedGraphics: boolean;
  notice: string | null;
  onToggleEnhancedGraphics: () => void;
  onResetCamera: () => void;
  onCopyInteractiveView: () => void;
  onCopyStaticView: () => void;
};

export function TouchViewerControls({
  copiedCameraLink,
  enhancedGraphics,
  notice,
  onToggleEnhancedGraphics,
  onResetCamera,
  onCopyInteractiveView,
  onCopyStaticView,
}: Props) {
  return (
    <div className="touch-ui pointer-events-auto absolute right-4 top-4 z-30 items-center gap-2">
      <TouchIconButton
        icon={<RtxIcon enabled={enhancedGraphics} className="h-8 w-10" />}
        onClick={onToggleEnhancedGraphics}
        aria-label="Przełącz ulepszone cieniowanie"
        aria-pressed={enhancedGraphics}
      />
      <TouchIconButton
        icon={<RotateCcw size={20} aria-hidden="true" />}
        onClick={onResetCamera}
        aria-label="Resetuj kamerę"
      />
      <TouchIconButton
        icon={<Video size={20} aria-hidden="true" />}
        onClick={onCopyInteractiveView}
        aria-label="Skopiuj link do widoku kamery"
        className={cn(
          copiedCameraLink === "interactive" && "border-primary/60 bg-primary/25 text-primary",
        )}
      />
      <TouchIconButton
        icon={<Image size={20} aria-hidden="true" />}
        onClick={onCopyStaticView}
        aria-label="Skopiuj link do animowanego tła"
        className={cn(
          copiedCameraLink === "static" && "border-primary/60 bg-primary/25 text-primary",
        )}
      />
      {notice !== null && (
        <div
          key={notice}
          role="status"
          className="snapshot-toast pointer-events-none fixed inset-0 z-50 grid place-items-center bg-black/50"
        >
          <span className="px-6 text-center text-body font-bold text-white text-shadow-label">
            {notice}
          </span>
        </div>
      )}
    </div>
  );
}
