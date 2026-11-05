export function ProgressBar({ percent, label, className = '' }: { percent: number; label: string; className?: string }) {
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={percent}
      className={`h-1.5 overflow-hidden rounded-full bg-stone-200 dark:bg-stone-800 ${className}`}
    >
      <div className="h-full rounded-full bg-indigo-500 transition-[width] duration-700 ease-out" style={{ width: `${percent}%` }} />
    </div>
  )
}
