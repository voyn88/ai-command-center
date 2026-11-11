import { useTranslation } from 'react-i18next'
import { toggleLang } from '../lib/i18n'

export default function LangToggle() {
  const { t, i18n } = useTranslation()
  return (
    <button
      type="button"
      onClick={toggleLang}
      className="glass"
      style={{
        padding: '0.35rem 0.75rem',
        color: 'var(--tx)',
        fontSize: '0.72rem',
        fontWeight: 600,
        letterSpacing: '0.04em',
        cursor: 'pointer',
        background: 'var(--glass)',
      }}
      aria-label={t('toggleLanguage')}
    >
      {i18n.language === 'ru' ? 'EN' : 'RU'}
    </button>
  )
}
