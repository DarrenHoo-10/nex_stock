import type { AssistantContext } from '../../../stores/planUiStore';
import type { Source } from '../../../api/financeAssistant';
export function financeSource(context?: AssistantContext): Source | undefined {
    if (!context || context.kind === 'none')
        return undefined;
    return { kind: context.held ? 'holding' : context.kind, ...(context.kind === 'stock' ? { code: context.stockCode ?? undefined, name: context.stockName ?? undefined } : { id: context.boardId ?? undefined, name: context.boardName ?? undefined }), tradeDate: context.tradeDate ?? undefined };
}
