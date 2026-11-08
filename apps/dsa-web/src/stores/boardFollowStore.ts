import { create } from 'zustand';
import { followBoard, getBoardFollows, unfollowBoard } from '../api/boards';
import { toApiErrorMessage } from '../api/error';
import type { BoardType, FollowedBoard } from '../types/boards';

interface BoardFollowState {
  status: 'idle' | 'loading' | 'ready' | 'error';
  error: string | null;
  tradeDate: string | null;
  boards: FollowedBoard[];
  /** Board ids with a follow / unfollow request in flight. */
  pending: ReadonlySet<string>;
  /** Load once (or again with `force`). */
  load: (force?: boolean) => Promise<void>;
  /** Follow or unfollow; resolves with an error message when the request failed. */
  toggle: (board: { id: string; name: string; boardType: BoardType }) => Promise<string | null>;
  isFollowed: (boardId: string) => boolean;
}

/** Followed boards shared by the boards page and the picks board picker (persisted by the backend). */
export const useBoardFollowStore = create<BoardFollowState>((set, get) => ({
  status: 'idle',
  error: null,
  tradeDate: null,
  boards: [],
  pending: new Set(),
  load: async (force = false) => {
    const { status } = get();
    if (!force && (status === 'loading' || status === 'ready')) return;
    set({ status: 'loading', error: null });
    try {
      const response = await getBoardFollows();
      set({ status: 'ready', boards: response.boards ?? [], tradeDate: response.tradeDate ?? null });
    } catch (err) {
      set({ status: 'error', error: toApiErrorMessage(err, '关注的板块读取失败') });
    }
  },
  toggle: async (board) => {
    const followed = get().isFollowed(board.id);
    set((state) => ({ pending: new Set(state.pending).add(board.id) }));
    try {
      const response = followed ? await unfollowBoard(board.id) : await followBoard(board.id);
      set({ status: 'ready', boards: response.boards ?? [], tradeDate: response.tradeDate ?? null });
      return null;
    } catch (err) {
      return toApiErrorMessage(err, followed ? '取消关注失败，请稍后重试' : '关注失败，请稍后重试');
    } finally {
      set((state) => {
        const pending = new Set(state.pending);
        pending.delete(board.id);
        return { pending };
      });
    }
  },
  isFollowed: (boardId) => get().boards.some((board) => board.id === boardId),
}));
