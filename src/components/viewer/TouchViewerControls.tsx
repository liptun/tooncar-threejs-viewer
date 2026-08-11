import { Camera, RotateCcw } from "lucide-react";
import { TouchIconButton } from "@/components/common/TouchIconButton";

type Props = {
  notice: string | null;
  onResetCamera: () => void;
  onCreateSnapshot: () => void;
};

export function TouchViewerControls({
  notice,
  onResetCamera,
  onCreateSnapshot,
}: Props) {
  return (
    <div className="touch-ui pointer-events-auto absolute right-4 top-4 z-30 items-center gap-2">
      <TouchIconButton
        icon={<RotateCcw size={20} aria-hidden="true" />}
        onClick={onResetCamera}
        aria-label="Resetuj kamerę"
      />
      <TouchIconButton
        icon={<Camera size={20} aria-hidden="true" />}
        onClick={onCreateSnapshot}
        aria-label="Skopiuj link do widoku kamery"
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
