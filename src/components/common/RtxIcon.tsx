type Props = {
  enabled: boolean;
  className?: string;
};

export function RtxIcon({ enabled, className }: Props) {
  return (
    <svg
      viewBox="0 0 42 32"
      className={className}
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
    >
      <path d="M5 2h37l-5 28H0L5 2Z" fill={enabled === true ? "#76b900" : "#080808"} />
      {enabled === false && <path d="m34 2 4 0-5 28h-4L34 2Z" fill="#76b900" />}
      <text
        x="19"
        y="14"
        fill="white"
        textAnchor="middle"
        fontFamily="Arial, sans-serif"
        fontSize="10"
        fontWeight="700"
        letterSpacing="0.5"
      >
        RTX
      </text>
      <text
        x="19"
        y="25"
        fill="white"
        textAnchor="middle"
        fontFamily="Arial, sans-serif"
        fontSize="10"
        fontWeight="800"
        letterSpacing="0.2"
      >
        {enabled === true ? "ON" : "OFF"}
      </text>
    </svg>
  );
}
