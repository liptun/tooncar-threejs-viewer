import { useEffect, type ReactNode } from "react";
import { ArrowUp, ChevronsUpDown, Mouse, Move } from "lucide-react";

type Props = {
  fadingOut: boolean;
  onDismiss: () => void;
};

function Key({ children, wide = false }: { children: ReactNode; wide?: boolean }) {
  return (
    <kbd
      className="grid shrink-0 place-items-center rounded-md border border-white/20 bg-base/70 px-2 text-xs font-semibold shadow-[0_2px_0_rgb(255_255_255/0.12)]"
      style={{ width: wide ? 96 : 32, height: 32 }}
    >
      {children}
    </kbd>
  );
}

export function DesktopControlsTutorial({ fadingOut, onDismiss }: Props) {
  useEffect(() => {
    const dismiss = () => onDismiss();
    window.addEventListener("pointerdown", dismiss, true);
    window.addEventListener("wheel", dismiss, { capture: true, passive: true });
    window.addEventListener("keydown", dismiss, true);
    return () => {
      window.removeEventListener("pointerdown", dismiss, true);
      window.removeEventListener("wheel", dismiss, true);
      window.removeEventListener("keydown", dismiss, true);
    };
  }, [onDismiss]);

  return (
    <div
      className={`desktop-ui pointer-events-none absolute inset-0 z-10 flex items-center justify-center bg-base/70 p-5 backdrop-blur-[2px] ${fadingOut ? "tutorial-overlay-exit" : "tutorial-overlay-enter"}`}
      role="status"
      aria-label="Podpowiedź sterowania kamerą"
    >
      <div className="w-full max-w-2xl text-shadow-label">
        <p
          className="text-center text-2xl font-semibold uppercase tracking-display text-primary"
          style={{ marginBottom: 48 }}
        >
          Sterowanie kamerą
        </p>

        <div className="flex items-center justify-center" style={{ gap: 96 }}>
          <div
            aria-label="Klawisze sterowania"
            className="flex flex-col items-center"
            style={{ width: 192, gap: 40 }}
          >
            <div className="flex w-full flex-col items-center">
              <p
                className="text-center text-sm font-semibold uppercase tracking-ui text-white/60"
                style={{ marginBottom: 12 }}
              >
                Góra / dół
              </p>
              <div className="grid" style={{ gridTemplateColumns: "32px 32px 32px", gap: 8 }}>
                <Key>Q</Key>
                <span />
                <Key>E</Key>
              </div>
            </div>
            <div className="flex w-full flex-col items-center">
              <p
                className="text-center text-sm font-semibold uppercase tracking-ui text-white/60"
                style={{ marginBottom: 12 }}
              >
                Poruszanie
              </p>
              <div className="grid" style={{ gridTemplateColumns: "32px 32px 32px", gap: 8 }}>
                <span />
                <Key>W</Key>
                <span />
                <Key>A</Key>
                <Key>S</Key>
                <Key>D</Key>
              </div>
            </div>
            <div className="flex w-full flex-col items-center">
              <p
                className="text-center text-sm font-semibold uppercase tracking-ui text-white/60"
                style={{ marginBottom: 12 }}
              >
                Sprint
              </p>
              <Key wide>
                <span className="flex items-center gap-2">
                  <ArrowUp size={17} strokeWidth={2.5} aria-hidden="true" />
                  Shift
                </span>
              </Key>
            </div>
          </div>

          <div className="flex w-60 flex-col items-center text-center">
            <p
              className="text-center text-sm font-semibold uppercase tracking-ui text-white/60"
              style={{ marginBottom: 16 }}
            >
              Mysz
            </p>
            <Mouse
              className="text-primary"
              style={{ width: 80, height: 104 }}
              strokeWidth={1.7}
              aria-hidden="true"
            />
            <div
              className="grid w-full"
              style={{ gridTemplateColumns: "112px 112px", gap: 16, marginTop: 24 }}
            >
              <div>
                <p className="flex items-center justify-center gap-2 text-lg font-semibold">
                  <Move size={18} className="text-primary" aria-hidden="true" />
                  Przeciągnij
                </p>
                <p className="mt-1.5 text-sm text-white/60">Rozglądanie</p>
              </div>
              <div>
                <p className="flex items-center justify-center gap-2 text-lg font-semibold">
                  <ChevronsUpDown size={18} className="text-primary" aria-hidden="true" />
                  Kółko
                </p>
                <p className="mt-1.5 text-sm text-white/60">Prędkość lotu</p>
              </div>
            </div>
          </div>
        </div>

        <p className="text-center text-base text-white/30" style={{ marginTop: 48 }}>
          Zacznij sterować, aby ukryć podpowiedź
        </p>
      </div>
    </div>
  );
}
