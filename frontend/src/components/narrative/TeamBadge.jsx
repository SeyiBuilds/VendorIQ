import { cn } from "@/lib/utils";

// Generic shield with the club's three-letter code. Not a crest.
export default function TeamBadge({ colors, size = 48, className }) {
  return (
    <svg
      width={size}
      height={size * 1.15}
      viewBox="0 0 40 46"
      className={cn("shrink-0 drop-shadow-[0_4px_10px_rgba(0,0,0,0.35)]", className)}
      role="img"
      aria-label={colors.code}
    >
      <path
        d="M20 1.5 37 7v14.5c0 10.6-7.1 18.7-17 23-9.9-4.3-17-12.4-17-23V7z"
        fill={colors.fill}
        stroke="rgba(255,255,255,0.28)"
        strokeWidth="1.5"
      />
      <path
        d="M20 5.5 33.5 10v11.5c0 8.4-5.5 15-13.5 18.6"
        fill="none"
        stroke="rgba(255,255,255,0.12)"
        strokeWidth="1.2"
      />
      <text
        x="20"
        y="26"
        textAnchor="middle"
        fontFamily="Manrope, system-ui, sans-serif"
        fontWeight="800"
        fontSize="11"
        letterSpacing="0.5"
        fill={colors.text}
      >
        {colors.code}
      </text>
    </svg>
  );
}
