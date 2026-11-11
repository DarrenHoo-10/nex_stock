import { useEffect, useId, useRef, useState, type CSSProperties } from 'react';
import type { Models } from '../../../api/financeAssistant';

const labels: Record<string, string> = { off: '关闭', low: '低', medium: '中', high: '高', xhigh: '很高', max: '最高' };
const rank = Object.keys(labels);

export function FinanceModelControls({ catalog, busy, onModel }: {
    catalog?: Models;
    busy: boolean;
    onModel: (connection: string, model: string, reasoning?: string) => Promise<void>;
}) {
    const id = useId(), wrap = useRef<HTMLDivElement>(null), trigger = useRef<HTMLButtonElement>(null);
    const [open, setOpen] = useState(false);
    const [pendingLevel, setPendingLevel] = useState<string>();
    const selected = catalog?.selection;
    const connection = catalog?.connections.find(item => item.id === selected?.selected_connection);
    const model = selected?.selected_model || connection?.defaultModel || '';
    const name = connection?.names?.[model] || model;
    const available = connection?.options[model]?.reasoning ?? [];
    const levels = ['off', ...rank.filter(level => level !== 'off' && available.includes(level))];
    const wanted = rank.indexOf(selected?.selected_reasoning || 'medium');
    const effective = selected?.selected_reasoning === 'off' || levels.length === 1 ? 'off' :
        levels.slice(1).reduce((best, option) => Math.abs(rank.indexOf(option) - wanted) < Math.abs(rank.indexOf(best) - wanted) ? option : best);
    useEffect(() => {
        if (!open) return;
        function dismiss(event: PointerEvent) {
            if (event.target instanceof Node && !wrap.current?.contains(event.target)) setOpen(false);
        }
        function escape(event: KeyboardEvent) {
            if (event.key === 'Escape') { setOpen(false); trigger.current?.focus(); }
        }
        document.addEventListener('pointerdown', dismiss);
        document.addEventListener('keydown', escape);
        return () => { document.removeEventListener('pointerdown', dismiss); document.removeEventListener('keydown', escape); };
    }, [open]);
    return <div className="finance-model-controls">
        <select aria-label="金融模型" title={name || '选择模型'} disabled={busy || !catalog?.connections.some(c => c.enabled && c.configured)}
            value={connection ? JSON.stringify([connection.id, selected?.selected_model ?? '']) : ''}
            onChange={event => { const [provider, value] = JSON.parse(event.target.value) as [string, string]; setOpen(false); void onModel(provider, value); }}>
            <option value="" disabled>选择模型</option>
            {catalog?.connections.filter(c => c.enabled && c.configured).map(c =>
                <optgroup key={c.id} label={c.name}>
                    <option value={JSON.stringify([c.id, ''])}>{c.names?.[c.defaultModel] || c.defaultModel}（默认）</option>
                    {Array.from(new Set([c.defaultModel, ...(c.models ?? []), ...Object.keys(c.options), ...(c.id === connection?.id ? [model] : [])])).filter(Boolean).map(value =>
                        <option key={value} value={JSON.stringify([c.id, value])}>{c.names?.[value] || value}</option>)}
                </optgroup>)}
        </select>
        <div className="finance-effort" ref={wrap}>
            <button type="button" ref={trigger} aria-label="金融思考强度" aria-expanded={open} aria-controls={id}
                disabled={busy || !connection || levels.length < 2} onClick={() => setOpen(!open)}>
                {connection ? '思考 · ' + labels[effective] : '思考强度'} ▾
            </button>
            {open && <div className="finance-effort-panel" id={id} role="group" aria-label="选择思考强度">
                <strong>{labels[pendingLevel ?? effective]}</strong><small>{name}</small>
                <div className="finance-effort-steps" role="radiogroup" aria-label="可用强度"
                    style={{ '--steps': levels.length, '--progress': (100 * levels.indexOf(pendingLevel ?? effective) / Math.max(1, levels.length - 1)) + '%' } as CSSProperties}>
                    {levels.map(level => <label key={level} title={labels[level]}><input type="radio" name={id} checked={(pendingLevel ?? effective) === level}
                        disabled={busy} aria-label={labels[level]} onChange={() => {
                            if (!connection) return;
                            setPendingLevel(level);
                            void onModel(connection.id, selected?.selected_model || '', level).finally(() => setPendingLevel(undefined));
                        }}/>
                        <span>{labels[level]}</span></label>)}
                </div>
            </div>}
        </div>
    </div>;
}
