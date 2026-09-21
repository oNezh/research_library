import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type AppSettings, type SettingsResponse } from '../lib/api'

type FormState = AppSettings

function toForm(s: AppSettings): FormState {
  return {
    ads: { ...s.ads },
    llm: { ...s.llm },
    embedding: { ...s.embedding },
    zotero: { ...s.zotero },
  }
}

export default function SettingsPage() {
  const qc = useQueryClient()
  const { data, isLoading } = useQuery({
    queryKey: ['settings'],
    queryFn: api.settings,
  })
  const [form, setForm] = useState<FormState | null>(null)
  const [testMsg, setTestMsg] = useState<string | null>(null)

  useEffect(() => {
    if (data?.settings) setForm(toForm(data.settings))
  }, [data])

  const save = useMutation({
    mutationFn: () => api.patchSettings(form!),
    onSuccess: (res: SettingsResponse) => {
      setForm(toForm(res.settings))
      qc.invalidateQueries({ queryKey: ['health'] })
      qc.invalidateQueries({ queryKey: ['settings'] })
    },
  })

  const test = useMutation({
    mutationFn: api.testSettings,
    onSuccess: (res) => {
      const lines = Object.entries(res.results).map(([k, v]) => `${k}: ${v}`)
      setTestMsg(lines.join('\n'))
    },
  })

  if (isLoading || !form) {
    return <div className="p-6 text-sm text-neutral-400">加载设置…</div>
  }

  const embProvider = form.embedding.provider

  return (
    <div className="h-full overflow-auto p-6">
      <div className="mx-auto max-w-2xl space-y-6">
        <div>
          <h1 className="text-xl font-bold">设置</h1>
          <p className="mt-1 text-sm text-neutral-400">配置 API 密钥，保存后立即生效，无需重启。</p>
        </div>

        <Section title="ADS">
          <Field label="API Token">
            <input
              type="password"
              value={form.ads.api_token}
              onChange={(e) => setForm({ ...form, ads: { ...form.ads, api_token: e.target.value } })}
              placeholder={data?.configured.ads ? '已配置（留空保持不变）' : 'ADS API Token'}
              className={inputCls}
            />
          </Field>
        </Section>

        <Section title="LLM">
          <Field label="Provider">
            <select
              value={form.llm.provider}
              onChange={(e) => setForm({ ...form, llm: { ...form.llm, provider: e.target.value } })}
              className={inputCls}
            >
              <option value="openai_compat">OpenAI 兼容</option>
              <option value="minimax">MiniMax</option>
            </select>
          </Field>
          <Field label="API Key">
            <input type="password" value={form.llm.api_key} onChange={(e) => setForm({ ...form, llm: { ...form.llm, api_key: e.target.value } })} placeholder="留空保持不变" className={inputCls} />
          </Field>
          <Field label="Base URL">
            <input value={form.llm.base_url} onChange={(e) => setForm({ ...form, llm: { ...form.llm, base_url: e.target.value } })} className={inputCls} />
          </Field>
          <Field label="Model">
            <input value={form.llm.model} onChange={(e) => setForm({ ...form, llm: { ...form.llm, model: e.target.value } })} className={inputCls} />
          </Field>
        </Section>

        <Section title="Embedding">
          <Field label="Provider">
            <select
              value={embProvider}
              onChange={(e) => setForm({ ...form, embedding: { ...form.embedding, provider: e.target.value } })}
              className={inputCls}
            >
              <option value="local_sentence_transformer">本地模型</option>
              <option value="openai_compat">在线 OpenAI 兼容</option>
              <option value="minimax">MiniMax</option>
            </select>
          </Field>
          {embProvider === 'local_sentence_transformer' ? (
            <>
              <Field label="本地模型 (HuggingFace ID)">
                <input
                  value={form.embedding.local_model}
                  onChange={(e) => setForm({ ...form, embedding: { ...form.embedding, local_model: e.target.value } })}
                  placeholder="Qwen/Qwen3-Embedding-4B"
                  className={inputCls}
                />
              </Field>
              <Field label="HF 缓存目录">
                <input
                  value={form.embedding.hf_home}
                  onChange={(e) => setForm({ ...form, embedding: { ...form.embedding, hf_home: e.target.value } })}
                  placeholder="/Users/you/program/qwen 或 ~/.cache/huggingface"
                  className={inputCls}
                />
              </Field>
              <Field label="设备">
                <select value={form.embedding.device} onChange={(e) => setForm({ ...form, embedding: { ...form.embedding, device: e.target.value } })} className={inputCls}>
                  <option value="cpu">CPU</option>
                  <option value="cuda">CUDA</option>
                  <option value="mps">MPS (Apple)</option>
                </select>
              </Field>
              <label className="flex items-center gap-2 text-sm text-neutral-600 dark:text-neutral-300">
                <input
                  type="checkbox"
                  checked={form.embedding.hf_offline === '1' || form.embedding.hf_offline === 'true'}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      embedding: { ...form.embedding, hf_offline: e.target.checked ? '1' : '0' },
                    })
                  }
                />
                离线模式（仅使用本地缓存，不访问 huggingface.co）
              </label>
              <p className="text-xs text-neutral-400">
                模型名须与缓存目录中已有模型一致；离线模式下无法自动下载。
              </p>
            </>
          ) : (
            <>
              <Field label="API Key">
                <input type="password" value={form.embedding.api_key} onChange={(e) => setForm({ ...form, embedding: { ...form.embedding, api_key: e.target.value } })} placeholder="留空保持不变" className={inputCls} />
              </Field>
              <Field label="Base URL">
                <input value={form.embedding.base_url} onChange={(e) => setForm({ ...form, embedding: { ...form.embedding, base_url: e.target.value } })} placeholder="https://api.openai.com/v1 或本地 http://127.0.0.1:..." className={inputCls} />
              </Field>
              <Field label="Model">
                <input value={form.embedding.model} onChange={(e) => setForm({ ...form, embedding: { ...form.embedding, model: e.target.value } })} className={inputCls} />
              </Field>
            </>
          )}
        </Section>

        <Section title="Zotero">
          <Field label="Library ID">
            <input value={form.zotero.library_id} onChange={(e) => setForm({ ...form, zotero: { ...form.zotero, library_id: e.target.value } })} className={inputCls} />
          </Field>
          <Field label="API Key">
            <input type="password" value={form.zotero.api_key} onChange={(e) => setForm({ ...form, zotero: { ...form.zotero, api_key: e.target.value } })} placeholder="留空保持不变" className={inputCls} />
          </Field>
          <Field label="Library Type">
            <select value={form.zotero.library_type} onChange={(e) => setForm({ ...form, zotero: { ...form.zotero, library_type: e.target.value } })} className={inputCls}>
              <option value="user">user</option>
              <option value="group">group</option>
            </select>
          </Field>
        </Section>

        <div className="flex flex-wrap gap-3">
          <button onClick={() => save.mutate()} disabled={save.isPending} className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50">
            {save.isPending ? '保存中…' : '保存设置'}
          </button>
          <button onClick={() => test.mutate()} disabled={test.isPending} className="rounded-md border border-neutral-300 px-4 py-2 text-sm hover:bg-neutral-50 dark:border-neutral-700 dark:hover:bg-neutral-800">
            {test.isPending ? '测试中…' : '测试连接'}
          </button>
        </div>
        {save.error && <p className="text-sm text-red-500">{String((save.error as Error).message)}</p>}
        {testMsg && <pre className="rounded-md bg-neutral-100 p-3 text-xs dark:bg-neutral-800">{testMsg}</pre>}
      </div>
    </div>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-neutral-200 bg-white p-4 dark:border-neutral-800 dark:bg-neutral-900">
      <h2 className="mb-3 text-sm font-bold">{title}</h2>
      <div className="space-y-3">{children}</div>
    </section>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs text-neutral-400">{label}</span>
      {children}
    </label>
  )
}

const inputCls =
  'w-full rounded-md border border-neutral-300 px-3 py-2 text-sm outline-none focus:border-blue-500 dark:border-neutral-700 dark:bg-neutral-800'
