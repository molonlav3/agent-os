import { create } from 'zustand'

/** The toolbar's attention panel: open or not. The truth badge opens it too. */
export const useAttentionPanel = create<{
  open: boolean
  setOpen(open: boolean): void
  toggle(): void
}>((set) => ({
  open: false,
  setOpen: (open) => set({ open }),
  toggle: () => set((s) => ({ open: !s.open })),
}))
