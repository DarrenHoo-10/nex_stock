import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { CurrentUserProvider } from '../../../../test-utils/currentUser';
import { adminUser, regularUser } from '../../../../test-utils/currentUserFixtures';
import type { CurrentUser } from '../../../../types/me';
import { BoardFocusStrip } from '../BoardFocusStrip';

function renderStrip(user: CurrentUser | null) {
  return render(
    <MemoryRouter>
      <CurrentUserProvider user={user}>
        <BoardFocusStrip
          recommendations={{
            status: 'ready',
            data: { status: 'ready', source: 'rule', generatedAt: null, items: [], note: null } as never,
          }}
          onRegenerate={vi.fn().mockResolvedValue(null)}
          followed={[]}
          followStatus="ready"
          followError={null}
          onRetryFollows={vi.fn()}
          activeId={null}
          onSelect={vi.fn()}
          onMessage={vi.fn()}
        />
      </CurrentUserProvider>
    </MemoryRouter>,
  );
}

describe('BoardFocusStrip recommendation refresh', () => {
  it('offers 重新生成 to an administrator', () => {
    renderStrip(adminUser);

    expect(screen.getByRole('button', { name: '重新生成' })).toBeInTheDocument();
  });

  it('offers 重新生成 in local mode', () => {
    renderStrip(null);

    expect(screen.getByRole('button', { name: '重新生成' })).toBeInTheDocument();
  });

  it('hides it from a regular user, because the refresh endpoint is administrator-only', () => {
    renderStrip(regularUser);

    expect(screen.queryByRole('button', { name: '重新生成' })).toBeNull();
    expect(screen.getByText('今日建议')).toBeInTheDocument();
  });
});
