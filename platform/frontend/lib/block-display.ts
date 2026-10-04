import type { Locale } from './i18n'

const typeLabels: Record<string, string> = {
  any: '任意类型', string: '文本', number: '数字', boolean: '是／否',
  object: '结构化对象', array: '列表', file: '文件', file_list: '文件列表',
}

export function blockPortType(valueType: string, locale: Locale = 'zh') {
  return locale === 'zh' ? typeLabels[valueType] || valueType : valueType
}
