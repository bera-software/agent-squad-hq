/**
 * Renders the shared Markdown tree as native <Text> elements. No HTML or
 * WebView is involved, and links open only when the parser marked them safe.
 */
import { Linking, StyleSheet, Text, View } from "react-native";
import { inlineText, parseMarkdown, type Block, type Inline } from "@squadhq/shared-api/markdown";
import { usePalette, type Palette } from "../theme";

function renderInline(nodes: Inline[], p: Palette, keyPrefix: string): React.ReactNode[] {
  return nodes.map((n, i) => {
    const key = `${keyPrefix}-${i}`;
    switch (n.type) {
      case "text":
        return n.text;
      case "code":
        return (
          <Text key={key} style={[s.inlineCode, { backgroundColor: p.codeBg }]}>
            {n.text}
          </Text>
        );
      case "strong":
        return (
          <Text key={key} style={s.strong}>
            {renderInline(n.children, p, key)}
          </Text>
        );
      case "em":
        return (
          <Text key={key} style={s.em}>
            {renderInline(n.children, p, key)}
          </Text>
        );
      case "link": {
        const href = n.href;
        if (!href) return <Text key={key}>{renderInline(n.children, p, key)}</Text>;
        return (
          <Text
            key={key}
            accessibilityRole="link"
            accessibilityHint={`Opens ${href}`}
            style={[s.link, { color: p.primary }]}
            onPress={() => void Linking.openURL(href)}
          >
            {renderInline(n.children, p, key)}
          </Text>
        );
      }
    }
  });
}

function renderBlock(b: Block, p: Palette, i: number) {
  const key = `b${i}`;
  const base = { color: p.text };
  switch (b.type) {
    case "heading":
      return (
        <Text key={key} accessibilityRole="header" style={[base, s.heading, b.level === 1 ? s.h1 : b.level === 2 ? s.h2 : s.h3]}>
          {renderInline(b.children, p, key)}
        </Text>
      );
    case "paragraph":
      return (
        <Text key={key} style={[base, s.p]}>
          {renderInline(b.children, p, key)}
        </Text>
      );
    case "quote":
      return (
        <View key={key} style={[s.quote, { borderLeftColor: p.border }]}>
          <Text style={[{ color: p.muted }, s.p]}>{renderInline(b.children, p, key)}</Text>
        </View>
      );
    case "code":
      return (
        <View key={key} style={[s.codeBlock, { backgroundColor: p.codeBg }]}>
          <Text style={[base, s.mono]}>{b.text}</Text>
        </View>
      );
    case "hr":
      return <View key={key} style={[s.hr, { backgroundColor: p.border }]} />;
    case "list":
      return (
        <View key={key} style={s.list} accessibilityRole="list">
          {b.items.map((item, j) => (
            <View key={`${key}-${j}`} style={s.li} accessibilityLabel={inlineText(item)}>
              <Text style={[base, s.bullet]}>{b.ordered ? `${j + 1}.` : "•"}</Text>
              <Text style={[base, s.p, s.liText]}>{renderInline(item, p, `${key}-${j}`)}</Text>
            </View>
          ))}
        </View>
      );
  }
}

export function Markdown({ source }: { source: string }) {
  const p = usePalette();
  return <View>{parseMarkdown(source).map((b, i) => renderBlock(b, p, i))}</View>;
}

const s = StyleSheet.create({
  heading: { fontWeight: "700", marginTop: 12, marginBottom: 6 },
  h1: { fontSize: 24 },
  h2: { fontSize: 20 },
  h3: { fontSize: 17 },
  p: { fontSize: 16, lineHeight: 23, marginBottom: 8 },
  strong: { fontWeight: "700" },
  em: { fontStyle: "italic" },
  link: { textDecorationLine: "underline" },
  inlineCode: { fontFamily: "Courier", fontSize: 15 },
  mono: { fontFamily: "Courier", fontSize: 14 },
  codeBlock: { padding: 10, borderRadius: 8, marginBottom: 8 },
  quote: { borderLeftWidth: 4, paddingLeft: 10, marginBottom: 8 },
  hr: { height: 1, marginVertical: 12 },
  list: { marginBottom: 8 },
  li: { flexDirection: "row" },
  bullet: { width: 22, fontSize: 16, lineHeight: 23 },
  liText: { flex: 1, marginBottom: 2 },
});
