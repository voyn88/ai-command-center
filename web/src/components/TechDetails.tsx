import type { ReactNode } from 'react'

export type TechItem = [label: string, value: ReactNode | null | undefined]

type TechDetailsProps = {
  title: string
  hint: string
  items: TechItem[]
  /** Server (owner) mode uses larger touch targets. */
  large?: boolean
}

// Native <details> disclosure for raw codes and ids: collapsed by default,
// keyboard reachable (Tab -> summary, Enter/Space), selectable text. Rows with
// an empty value are not rendered; with no rows the whole widget is omitted.
export default function TechDetails({ title, hint, items, large = false }: TechDetailsProps) {
  const rows = items.filter(([, value]) => value !== null && value !== undefined && value !== '')
  if (rows.length === 0) return null
  return (
    <details className={`tech-details${large ? ' tech-details--large' : ''}`}>
      <summary>{title}</summary>
      <dl>
        {rows.map(([label, value], i) => (
          <div key={`${label}-${i}`}>
            <dt>{label}:</dt> <dd>{value}</dd>
          </div>
        ))}
      </dl>
      <p>{hint}</p>
    </details>
  )
}
