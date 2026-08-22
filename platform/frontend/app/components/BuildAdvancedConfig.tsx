/**
 * 构建高级配置面板：把 BuildRequest 的可调参数（回合/修复/时限/规划/发布）
 * 在发起构建前显式暴露给用户。折叠时 summary 常驻一行当前配置摘要，
 * 让"这次构建用什么预算"始终可见，而不是静默吃后端默认值。
 */
'use client'

import styles from './build-advanced-config.module.css'

export type PlanningMode = 'auto' | 'required' | 'disabled'

export type BuildOptions = {
  maxTurns: string
  maxRepairCycles: string
  /** 空字符串 = 不设整次构建的超时上限（发送 null）。 */
  maxElapsedSeconds: string
  planningMode: PlanningMode
  autoPublish: boolean
}

/** 与后端 BuildRequest 的字段默认值保持一致（workflow_models.py）。 */
export const BUILD_OPTION_BOUNDS = {
  maxTurns: { default: 36, min: 5, max: 200 },
  maxRepairCycles: { default: 4, min: 1, max: 30 },
  maxElapsedSeconds: { default: 480, min: 0.001, max: 86_400 },
} as const

export function defaultBuildOptions(autoPublish: boolean): BuildOptions {
  return {
    maxTurns: String(BUILD_OPTION_BOUNDS.maxTurns.default),
    maxRepairCycles: String(BUILD_OPTION_BOUNDS.maxRepairCycles.default),
    maxElapsedSeconds: String(BUILD_OPTION_BOUNDS.maxElapsedSeconds.default),
    planningMode: 'auto',
    autoPublish,
  }
}

const LABELS = {
  zh: {
    summary: '高级配置',
    hint: '不改就按平台默认值构建',
    maxTurns: '最大回合数',
    maxTurnsHelp: '莉莉丝最多工作多少轮（5–200）',
    maxRepairCycles: '修复循环上限',
    maxRepairCyclesHelp: '测试失败后最多修几轮（1–30）',
    maxElapsedSeconds: '构建时限（秒）',
    maxElapsedSecondsHelp: '留空 = 不设整体超时',
    planningMode: '规划模式',
    planningModeHelp: '复杂需求建议先出施工计划',
    planningAuto: '自动（莉莉丝自行判断）',
    planningRequired: '必须先出计划',
    planningDisabled: '关闭计划工具',
    autoPublish: '测试通过后自动发布',
    noDeadline: '不限时',
    digestTurns: (v: string) => `${v} 轮`,
    digestRepairs: (v: string) => `修复 ${v}`,
    digestDeadline: (v: string) => (v ? `${v} 秒` : '不限时'),
    digestPlanning: { auto: '规划自动', required: '必须规划', disabled: '不规划' } as Record<PlanningMode, string>,
    digestPublish: (v: boolean) => (v ? '自动发布' : '手动发布'),
    invalidTurns: '最大回合数需为 5–200 的整数',
    invalidRepairs: '修复循环上限需为 1–30 的整数',
    invalidDeadline: '构建时限需为 0–86400 之间的数字，或留空表示不限时',
  },
  en: {
    summary: 'Advanced settings',
    hint: 'Platform defaults apply unless changed',
    maxTurns: 'Max turns',
    maxTurnsHelp: 'Upper bound of builder turns (5–200)',
    maxRepairCycles: 'Max repair cycles',
    maxRepairCyclesHelp: 'Repair rounds after failing tests (1–30)',
    maxElapsedSeconds: 'Build deadline (seconds)',
    maxElapsedSecondsHelp: 'Empty = no whole-build timeout',
    planningMode: 'Planning mode',
    planningModeHelp: 'Complex builds benefit from an upfront plan',
    planningAuto: 'Auto (Lilies decides)',
    planningRequired: 'Plan required first',
    planningDisabled: 'Planning disabled',
    autoPublish: 'Auto-publish after green tests',
    noDeadline: 'No deadline',
    digestTurns: (v: string) => `${v} turns`,
    digestRepairs: (v: string) => `${v} repairs`,
    digestDeadline: (v: string) => (v ? `${v}s` : 'no deadline'),
    digestPlanning: { auto: 'plan auto', required: 'plan required', disabled: 'no plan' } as Record<PlanningMode, string>,
    digestPublish: (v: boolean) => (v ? 'auto-publish' : 'manual publish'),
    invalidTurns: 'Max turns must be an integer between 5 and 200',
    invalidRepairs: 'Max repair cycles must be an integer between 1 and 30',
    invalidDeadline: 'Build deadline must be between 0 and 86400 seconds, or empty for no deadline',
  },
} as const

type Locale = keyof typeof LABELS

/**
 * 把面板选项转成 BuildRequest 的请求体字段。校验失败返回 error（业主语言），
 * 成功返回 payload；时限留空会显式发送 null（真正的不限时，而非静默回落到默认 480 秒）。
 */
export function buildOptionsPayload(
  options: BuildOptions,
  locale: Locale = 'zh',
): { payload: Record<string, unknown>; error?: undefined } | { payload?: undefined; error: string } {
  const t = LABELS[locale]
  const turns = Number(options.maxTurns)
  const turnBounds = BUILD_OPTION_BOUNDS.maxTurns
  if (!Number.isInteger(turns) || turns < turnBounds.min || turns > turnBounds.max) {
    return { error: t.invalidTurns }
  }
  const repairs = Number(options.maxRepairCycles)
  const repairBounds = BUILD_OPTION_BOUNDS.maxRepairCycles
  if (!Number.isInteger(repairs) || repairs < repairBounds.min || repairs > repairBounds.max) {
    return { error: t.invalidRepairs }
  }
  const deadlineText = options.maxElapsedSeconds.trim()
  let deadline: number | null = null
  if (deadlineText) {
    deadline = Number(deadlineText)
    const bounds = BUILD_OPTION_BOUNDS.maxElapsedSeconds
    if (Number.isNaN(deadline) || deadline < bounds.min || deadline > bounds.max) {
      return { error: t.invalidDeadline }
    }
  }
  return {
    payload: {
      auto_publish: options.autoPublish,
      max_turns: turns,
      max_repair_cycles: repairs,
      max_elapsed_seconds: deadline,
      planning_mode: options.planningMode,
    },
  }
}

export default function BuildAdvancedConfig({
  value,
  onChange,
  locale = 'zh',
  tone = 'light',
  disabled = false,
}: {
  value: BuildOptions
  onChange: (next: BuildOptions) => void
  locale?: Locale
  tone?: 'light' | 'dark'
  disabled?: boolean
}) {
  const t = LABELS[locale]
  const digest = [
    t.digestTurns(value.maxTurns),
    t.digestRepairs(value.maxRepairCycles),
    t.digestDeadline(value.maxElapsedSeconds.trim()),
    t.digestPlanning[value.planningMode],
    t.digestPublish(value.autoPublish),
  ].join(' · ')

  function patch(partial: Partial<BuildOptions>) {
    onChange({ ...value, ...partial })
  }

  return (
    <details className={`${styles.panel} ${tone === 'dark' ? styles.dark : ''}`} data-build-advanced="panel">
      <summary>
        <b>{t.summary}</b>
        <span data-build-advanced="digest">{digest}</span>
        <small>{t.hint}</small>
      </summary>
      <div className={styles.grid}>
        <label>
          <span>{t.maxTurns}<em>{t.maxTurnsHelp}</em></span>
          <input
            data-build-advanced="max-turns"
            disabled={disabled}
            max={BUILD_OPTION_BOUNDS.maxTurns.max}
            min={BUILD_OPTION_BOUNDS.maxTurns.min}
            onChange={event => patch({ maxTurns: event.target.value })}
            step={1}
            type="number"
            value={value.maxTurns}
          />
        </label>
        <label>
          <span>{t.maxRepairCycles}<em>{t.maxRepairCyclesHelp}</em></span>
          <input
            data-build-advanced="max-repair-cycles"
            disabled={disabled}
            max={BUILD_OPTION_BOUNDS.maxRepairCycles.max}
            min={BUILD_OPTION_BOUNDS.maxRepairCycles.min}
            onChange={event => patch({ maxRepairCycles: event.target.value })}
            step={1}
            type="number"
            value={value.maxRepairCycles}
          />
        </label>
        <label>
          <span>{t.maxElapsedSeconds}<em>{t.maxElapsedSecondsHelp}</em></span>
          <input
            data-build-advanced="max-elapsed-seconds"
            disabled={disabled}
            max={BUILD_OPTION_BOUNDS.maxElapsedSeconds.max}
            min={0}
            onChange={event => patch({ maxElapsedSeconds: event.target.value })}
            placeholder={t.noDeadline}
            type="number"
            value={value.maxElapsedSeconds}
          />
        </label>
        <label>
          <span>{t.planningMode}<em>{t.planningModeHelp}</em></span>
          <select
            data-build-advanced="planning-mode"
            disabled={disabled}
            onChange={event => patch({ planningMode: event.target.value as PlanningMode })}
            value={value.planningMode}
          >
            <option value="auto">{t.planningAuto}</option>
            <option value="required">{t.planningRequired}</option>
            <option value="disabled">{t.planningDisabled}</option>
          </select>
        </label>
        <label className={styles.toggle}>
          <input
            checked={value.autoPublish}
            data-build-advanced="auto-publish"
            disabled={disabled}
            onChange={event => patch({ autoPublish: event.target.checked })}
            type="checkbox"
          />
          <span>{t.autoPublish}</span>
        </label>
      </div>
    </details>
  )
}
