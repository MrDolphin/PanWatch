import { useState, useEffect } from 'react'
import { fetchAPI } from '@panwatch/api'

const EVENT = 'panwatch:avatar-changed'
let cache: string | null = null
let inflight: Promise<string> | null = null

function load(): Promise<string> {
  if (cache !== null) return Promise.resolve(cache)
  if (!inflight) {
    inflight = fetchAPI<{ value: string }>('/settings/avatar')
      .then(r => {
        cache = r?.value || ''
        return cache
      })
      .catch(() => {
        cache = ''
        return ''
      })
  }
  return inflight
}

/**
 * 保存头像(传空字符串清空)并广播,使所有头像处即时更新。
 * 走通用 PUT /settings/{key}(catch-all)写入 ui_avatar:不依赖新路由注册顺序,
 * 读取则用 GET /settings/avatar(单独读 ui_avatar)。
 */
export async function saveAvatar(value: string): Promise<void> {
  await fetchAPI('/settings/ui_avatar', { method: 'PUT', body: JSON.stringify({ value }) })
  cache = value
  window.dispatchEvent(new CustomEvent<string>(EVENT, { detail: value }))
}

/** 当前头像(data URL 或图片地址),未设置为空串。跨组件即时同步。 */
export function useAvatar(): string {
  const [avatar, setAvatar] = useState<string>(cache ?? '')
  useEffect(() => {
    let alive = true
    load().then(v => {
      if (alive) setAvatar(v)
    })
    const onChange = (e: Event) => setAvatar((e as CustomEvent<string>).detail ?? '')
    window.addEventListener(EVENT, onChange)
    return () => {
      alive = false
      window.removeEventListener(EVENT, onChange)
    }
  }, [])
  return avatar
}

/**
 * 把上传的图片文件压缩为 128×128 的方形 JPEG data URL(居中裁剪),
 * 控制体积(约 10-20KB),避免大 base64 撑爆设置存储。
 */
export function fileToAvatarDataUrl(file: File, size = 128): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(new Error('读取文件失败'))
    reader.onload = () => {
      const img = new Image()
      img.onerror = () => reject(new Error('图片解析失败'))
      img.onload = () => {
        const canvas = document.createElement('canvas')
        canvas.width = size
        canvas.height = size
        const ctx = canvas.getContext('2d')
        if (!ctx) {
          reject(new Error('canvas 不可用'))
          return
        }
        // 居中裁剪填满方形
        const scale = Math.max(size / img.width, size / img.height)
        const w = img.width * scale
        const h = img.height * scale
        ctx.drawImage(img, (size - w) / 2, (size - h) / 2, w, h)
        resolve(canvas.toDataURL('image/jpeg', 0.85))
      }
      img.src = reader.result as string
    }
    reader.readAsDataURL(file)
  })
}
