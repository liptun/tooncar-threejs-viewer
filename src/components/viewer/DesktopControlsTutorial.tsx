import { useEffect, useId, useRef } from "react";
import { Mouse, X } from "lucide-react";

type Props = {
  onComplete: () => void;
};

const keyGroups = [
  { keys: ["W", "S"], description: "Lot do przodu i do tyłu" },
  { keys: ["A", "D"], description: "Lot w lewo i w prawo" },
  { keys: ["Q", "E"], description: "Lot w dół i w górę" },
  { keys: ["Shift"], description: "Sprint" },
];

export function DesktopControlsTutorial({ onComplete }: Props) {
  const titleId = useId();
  const descriptionId = useId();
  const completeButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    completeButtonRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      const controlsCamera = [
        "KeyW",
        "KeyA",
        "KeyS",
        "KeyD",
        "KeyQ",
        "KeyE",
        "ShiftLeft",
        "ShiftRight",
      ].includes(event.code);
      if (event.key === "Escape" || controlsCamera === true) {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
      if (event.key === "Escape") onComplete();
    };
    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [onComplete]);

  return (
    <div className="desktop-ui pointer-events-auto absolute inset-0 z-[70] flex items-center justify-center bg-base/75 p-5 backdrop-blur-sm">
      <section
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        className="relative w-full max-w-xl overflow-hidden rounded-2xl border border-white/15 bg-panel/95 p-6 shadow-2xl sm:p-8"
      >
        <button
          type="button"
          onClick={onComplete}
          className="absolute right-4 top-4 grid size-9 place-items-center rounded-full text-white/60 transition-colors hover:bg-white/10 hover:text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
          aria-label="Zamknij tutorial"
        >
          <X size={19} aria-hidden="true" />
        </button>

        <p className="mb-2 text-xs font-semibold uppercase tracking-display text-primary">
          Sterowanie kamerą
        </p>
        <h2 id={titleId} className="pr-8 text-2xl font-semibold sm:text-3xl">
          Rozejrzyj się po trasie
        </h2>
        <p id={descriptionId} className="mt-2 text-sm leading-6 text-white/65">
          Użyj myszy i klawiatury, aby swobodnie latać po mapie.
        </p>

        <div className="mt-6 grid gap-3 sm:grid-cols-2">
          <div className="flex items-center gap-4 rounded-xl border border-white/10 bg-white/5 p-4">
            <Mouse className="shrink-0 text-primary" size={28} aria-hidden="true" />
            <div>
              <p className="font-medium">Kliknij i ruszaj myszą</p>
              <p className="mt-1 text-xs text-white/55">Obrót kamery</p>
            </div>
          </div>
          <div className="flex items-center gap-4 rounded-xl border border-white/10 bg-white/5 p-4">
            <Mouse className="shrink-0 text-primary" size={28} aria-hidden="true" />
            <div>
              <p className="font-medium">Kółko myszy</p>
              <p className="mt-1 text-xs text-white/55">Zmiana prędkości lotu</p>
            </div>
          </div>
          {keyGroups.map(({ keys, description }) => (
            <div
              key={description}
              className="flex items-center gap-4 rounded-xl border border-white/10 bg-white/5 p-4"
            >
              <div className="flex min-w-20 gap-1.5">
                {keys.map((key) => (
                  <kbd
                    key={key}
                    className="grid h-8 min-w-8 place-items-center rounded-md border border-white/20 bg-base/70 px-2 text-xs font-semibold shadow-[0_2px_0_rgb(255_255_255/0.12)]"
                  >
                    {key}
                  </kbd>
                ))}
              </div>
              <p className="text-sm text-white/75">{description}</p>
            </div>
          ))}
        </div>

        <button
          ref={completeButtonRef}
          type="button"
          onClick={onComplete}
          className="mt-7 w-full rounded-xl bg-primary px-5 py-3 text-sm font-semibold text-base transition-[filter,transform] hover:brightness-110 active:scale-[0.99] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white"
        >
          Rozumiem, zaczynamy
        </button>
      </section>
    </div>
  );
}
