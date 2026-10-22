import { useColorScheme } from "react-native";

export interface Palette {
  bg: string;
  card: string;
  text: string;
  muted: string;
  border: string;
  primary: string;
  onPrimary: string;
  danger: string;
  onDanger: string;
  success: string;
  warning: string;
  codeBg: string;
}

// Contrast checked against WCAG 2.2 AA (>= 4.5:1 for body text) in both schemes.
export const light: Palette = {
  bg: "#f6f7f9",
  card: "#ffffff",
  text: "#111827",
  muted: "#4b5563",
  border: "#d1d5db",
  primary: "#1d4ed8",
  onPrimary: "#ffffff",
  danger: "#b91c1c",
  onDanger: "#ffffff",
  success: "#047857",
  warning: "#92400e",
  codeBg: "#eef0f3",
};

export const dark: Palette = {
  bg: "#0b0f17",
  card: "#151b26",
  text: "#f3f4f6",
  muted: "#a1a8b3",
  border: "#2d3748",
  primary: "#60a5fa",
  onPrimary: "#0b0f17",
  danger: "#f87171",
  onDanger: "#0b0f17",
  success: "#34d399",
  warning: "#fbbf24",
  codeBg: "#1f2733",
};

export function usePalette(): Palette {
  return useColorScheme() === "dark" ? dark : light;
}

/** Minimum touch target (iOS HIG 44pt, Material 48dp). */
export const TOUCH = 48;
