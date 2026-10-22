import type { ReactNode } from "react";
import { ActivityIndicator, Pressable, StyleSheet, Text, TextInput, View, type TextInputProps } from "react-native";
import { TOUCH, usePalette } from "../theme";

type Variant = "primary" | "danger" | "secondary";

export function Button(props: {
  label: string;
  onPress: () => void;
  variant?: Variant;
  disabled?: boolean;
  busy?: boolean;
  accessibilityHint?: string;
  testID?: string;
}) {
  const p = usePalette();
  const variant = props.variant ?? "primary";
  const bg = variant === "primary" ? p.primary : variant === "danger" ? p.danger : p.card;
  const fg = variant === "primary" ? p.onPrimary : variant === "danger" ? p.onDanger : p.text;
  const disabled = props.disabled || props.busy;
  return (
    <Pressable
      testID={props.testID}
      accessibilityRole="button"
      accessibilityLabel={props.label}
      accessibilityHint={props.accessibilityHint}
      accessibilityState={{ disabled: !!disabled, busy: !!props.busy }}
      disabled={disabled}
      onPress={props.onPress}
      style={({ pressed }) => [
        styles.button,
        { backgroundColor: bg, borderColor: variant === "secondary" ? p.border : bg, opacity: disabled ? 0.55 : pressed ? 0.8 : 1 },
      ]}
    >
      {props.busy ? <ActivityIndicator color={fg} /> : <Text style={[styles.buttonText, { color: fg }]}>{props.label}</Text>}
    </Pressable>
  );
}

export function Field(props: TextInputProps & { label: string; hint?: string }) {
  const p = usePalette();
  const { label, hint, style, ...rest } = props;
  return (
    <View style={styles.field}>
      <Text style={[styles.label, { color: p.text }]} nativeID={`${label}-label`}>
        {label}
      </Text>
      <TextInput
        accessibilityLabel={label}
        accessibilityHint={hint}
        placeholderTextColor={p.muted}
        style={[styles.input, { color: p.text, borderColor: p.border, backgroundColor: p.card }, style]}
        {...rest}
      />
      {hint ? <Text style={[styles.hint, { color: p.muted }]}>{hint}</Text> : null}
    </View>
  );
}

export function ErrorBanner({ message }: { message: string | null }) {
  const p = usePalette();
  if (!message) return null;
  return (
    <View accessibilityRole="alert" accessibilityLiveRegion="polite" style={[styles.banner, { borderColor: p.danger }]}>
      <Text style={{ color: p.danger }}>{message}</Text>
    </View>
  );
}

export function Card({ children }: { children: ReactNode }) {
  const p = usePalette();
  return <View style={[styles.card, { backgroundColor: p.card, borderColor: p.border }]}>{children}</View>;
}

export function Empty({ text }: { text: string }) {
  const p = usePalette();
  return <Text style={[styles.empty, { color: p.muted }]}>{text}</Text>;
}

export function Badge({ text, tone }: { text: string; tone?: "warning" | "danger" | "success" | "muted" }) {
  const p = usePalette();
  const color = tone === "warning" ? p.warning : tone === "danger" ? p.danger : tone === "success" ? p.success : p.muted;
  return (
    <View style={[styles.badge, { borderColor: color }]}>
      <Text style={[styles.badgeText, { color }]}>{text}</Text>
    </View>
  );
}

export const styles = StyleSheet.create({
  button: {
    minHeight: TOUCH,
    paddingHorizontal: 18,
    borderRadius: 10,
    borderWidth: 1,
    alignItems: "center",
    justifyContent: "center",
  },
  buttonText: { fontSize: 16, fontWeight: "600" },
  field: { marginBottom: 14 },
  label: { fontSize: 15, fontWeight: "600", marginBottom: 6 },
  input: { minHeight: TOUCH, borderWidth: 1, borderRadius: 10, paddingHorizontal: 12, fontSize: 16 },
  hint: { fontSize: 13, marginTop: 4 },
  banner: { borderWidth: 1, borderRadius: 10, padding: 12, marginBottom: 12 },
  card: { borderWidth: 1, borderRadius: 12, padding: 14, marginBottom: 10 },
  empty: { textAlign: "center", padding: 24, fontSize: 15 },
  badge: { borderWidth: 1, borderRadius: 999, paddingHorizontal: 8, paddingVertical: 2, alignSelf: "flex-start" },
  badgeText: { fontSize: 12, fontWeight: "600" },
});
