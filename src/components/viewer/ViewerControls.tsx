import { Camera, Maximize, RotateCcw } from "lucide-react";
import { ExpandableIconButton } from "@/components/common/ExpandableIconButton";
import { cn } from "@/lib/cn";

type Props = {
  snapshotCopied: boolean;
  onResetCamera: () => void;
  onCreateSnapshot: () => void;
};

export function ViewerControls({ snapshotCopied, onResetCamera, onCreateSnapshot }: Props) {
  return (
    <div
      className={cn(
        "desktop-ui pointer-events-auto absolute right-7 top-7 z-20",
        "flex items-center justify-end gap-2",
        "max-sm:right-4 max-sm:top-4",
      )}
    >
      <ExpandableIconButton
        icon={<RotateCcw size={17} />}
        label="Resetuj kamerę"
        expandedClassName="hover:w-40"
        labelClassName="group-hover:max-w-28"
        onClick={onResetCamera}
        aria-label="Resetuj kamerę"
        title="Resetuj kamerę"
      />
      <ExpandableIconButton
        icon={<Camera size={17} />}
        label={snapshotCopied === true ? "Skopiowano" : "Skopiuj widok"}
        onClick={onCreateSnapshot}
        aria-label="Skopiuj link do widoku kamery"
        title={snapshotCopied === true ? "Skopiowano link" : "Skopiuj link do widoku"}
        className={cn(snapshotCopied === true && "border-primary/60 bg-primary/25 text-primary")}
      />
      <ExpandableIconButton
        icon={<Maximize size={17} />}
        label="Pełny ekran"
        onClick={() => document.documentElement.requestFullscreen?.()}
        aria-label="Tryb pełnoekranowy"
        title="Pełny ekran"
      />
    </div>
  );
}
