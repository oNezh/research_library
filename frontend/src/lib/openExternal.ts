export async function openExternalUrl(url: string): Promise<void> {
  try {
    const { isTauri } = await import('@tauri-apps/api/core')
    if (isTauri()) {
      const { open } = await import('@tauri-apps/plugin-shell')
      await open(url)
      return
    }
  } catch {
    /* browser or plugin unavailable */
  }
  window.open(url, '_blank', 'noopener,noreferrer')
}
