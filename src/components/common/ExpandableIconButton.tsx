import type { ButtonHTMLAttributes, ReactNode } from 'react'

type Props = ButtonHTMLAttributes<HTMLButtonElement> & {
  icon: ReactNode
  label: string
  expandedClassName?: string
  labelClassName?: string
}

export function ExpandableIconButton({
  icon,
  label,
  expandedClassName = 'hover:w-36',
  labelClassName = 'group-hover:max-w-24',
  className = '',
  ...buttonProps
}: Props) {
  return (
    <button
      type="button"
      className={`group flex h-10 w-10 cursor-pointer items-center justify-center overflow-hidden whitespace-nowrap rounded-xl border border-white/10 bg-black/30 px-2.5 text-white/75 backdrop-blur-sm transition-[width,color,background-color,border-color] duration-300 hover:bg-[#10162d]/90 hover:text-white ${expandedClassName} ${className}`}
      {...buttonProps}
    >
      <span className="grid shrink-0 place-items-center">{icon}</span>
      <span className={`ml-0 max-w-0 overflow-hidden text-[10px] font-bold uppercase tracking-[.08em] opacity-0 [text-shadow:0_1px_3px_rgba(0,0,0,.95)] transition-[max-width,margin,opacity] duration-300 group-hover:ml-2 group-hover:opacity-100 ${labelClassName}`}>
        {label}
      </span>
    </button>
  )
}
