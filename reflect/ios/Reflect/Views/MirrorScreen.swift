import SwiftUI
import UIKit

/// Screen 2: the live Claude window, full screen in portrait, with the control bar.
struct MirrorScreen: View {
    @Environment(AppModel.self) private var model
    let connection: MirrorConnection

    @State private var showKeyboard = false
    @State private var draft = ""
    @FocusState private var typing: Bool

    var body: some View {
        VStack(spacing: 0) {
            GeometryReader { geometry in
                ZStack {
                    MirrorSurface(
                        image: connection.image,
                        onTap: { connection.tap($0) },
                        onLongPress: { connection.longPress($0) },
                        onScroll: { connection.scroll(at: $0, by: $1) }
                    )
                    StateOverlay(connection: connection)
                }
                .onAppear { reportViewport(geometry.size) }
                .onChange(of: geometry.size) { _, size in reportViewport(size) }
            }
            .overlay(alignment: .topTrailing) {
                StatsBadge(fps: connection.fps, latencyMs: connection.latencyMs)
                    .padding(.top, 4)
                    .padding(.trailing, 8)
                    .allowsHitTesting(false)
            }

            if showKeyboard {
                KeyboardBar(
                    draft: $draft,
                    focus: $typing,
                    onSend: {
                        connection.type(draft)
                        draft = ""
                    },
                    onReturn: {
                        connection.type(draft)
                        connection.key("enter")
                        draft = ""
                        Task { @MainActor in typing = true }
                    },
                    onBackspace: { connection.key("backspace") }
                )
                .transition(.move(edge: .bottom).combined(with: .opacity))
            }

            ControlBar(connection: connection, showKeyboard: $showKeyboard) {
                model.disconnect()
            }
        }
        .background(Color.black.ignoresSafeArea())
        .statusBarHidden()
        .persistentSystemOverlays(.hidden)
        .animation(.easeOut(duration: 0.2), value: showKeyboard)
        .onChange(of: showKeyboard) { _, shown in typing = shown }
        .onAppear { UIApplication.shared.isIdleTimerDisabled = true }
        .onDisappear { UIApplication.shared.isIdleTimerDisabled = false }
    }

    /// The desktop window takes the shape of the mirror area (ignored while the keyboard shrinks it).
    private func reportViewport(_ size: CGSize) {
        guard !showKeyboard else { return }
        connection.updateViewport(size)
    }
}

// MARK: - Overlays

private struct StateOverlay: View {
    let connection: MirrorConnection

    var body: some View {
        switch connection.phase {
        case .needsPairing:
            ZStack {
                Color.black.opacity(0.85)
                PairingPanel(connection: connection)
            }
        case .connecting:
            if connection.image == nil {
                MessageCard(symbol: nil, title: "Connecting to \(connection.helperName)\u{2026}", detail: nil)
            }
        case .reconnecting(let attempt):
            VStack {
                Banner(text: attempt > 2 ? "Reconnecting\u{2026} (\(connection.lastProblem ?? "no answer"))" : "Reconnecting\u{2026}")
                Spacer()
            }
        case .streaming:
            if let status = connection.status, !status.claudeRunning {
                dimmed(MessageCard(symbol: "macwindow.badge.plus", title: status.message.isEmpty ? "Claude desktop not running" : status.message,
                                   detail: "Open the Claude app on \(connection.helperName). The picture appears here by itself."))
            } else if let status = connection.status, !status.streaming {
                dimmed(MessageCard(symbol: "pause.circle", title: "Paused", detail: "Choose Start in the Reflect menu on the computer."))
            } else if connection.image == nil {
                MessageCard(symbol: nil, title: connection.status?.message.isEmpty == false ? connection.status!.message : "Waiting for the first picture\u{2026}", detail: nil)
            }
        case .stopped:
            EmptyView()
        }
    }

    private func dimmed<V: View>(_ content: V) -> some View {
        ZStack {
            Color.black.opacity(0.75)
            content
        }
    }
}

private struct MessageCard: View {
    let symbol: String?
    let title: String
    let detail: String?

    var body: some View {
        VStack(spacing: 14) {
            if let symbol {
                Image(systemName: symbol).font(.system(size: 40)).foregroundStyle(.secondary)
            } else {
                ProgressView().controlSize(.large)
            }
            Text(title).font(.headline).multilineTextAlignment(.center)
            if let detail {
                Text(detail).font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
            }
        }
        .padding(28)
        .frame(maxWidth: 340)
    }
}

private struct Banner: View {
    let text: String

    var body: some View {
        HStack(spacing: 8) {
            ProgressView().controlSize(.small)
            Text(text).font(.footnote.weight(.medium)).lineLimit(2)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .background(.ultraThinMaterial, in: Capsule())
        .padding(.top, 8)
    }
}

private struct StatsBadge: View {
    let fps: Int
    let latencyMs: Int?

    var body: some View {
        Text(latencyMs.map { "\(fps) fps \u{00B7} \($0) ms" } ?? "\(fps) fps")
            .font(.caption2.monospacedDigit().weight(.medium))
            .foregroundStyle(.white.opacity(0.85))
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .background(Color.black.opacity(0.55), in: Capsule())
    }
}

// MARK: - Pairing

private struct PairingPanel: View {
    let connection: MirrorConnection
    @State private var code = ""
    @FocusState private var focused: Bool

    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "lock.iphone").font(.system(size: 44)).foregroundStyle(.tint)
            Text("Pair with \(connection.helperName)").font(.title3.bold()).multilineTextAlignment(.center)
            Text(connection.pairingOpen
                 ? "Enter the 6-digit pairing code shown on the computer (in the Reflect console window and tray menu)."
                 : "On the computer, choose \u{201C}Pair a new phone\u{201D} in the Reflect tray menu, then enter the code it shows.")
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            TextField("000000", text: $code)
                .keyboardType(.numberPad)
                .textContentType(.oneTimeCode)
                .font(.system(size: 34, weight: .semibold, design: .monospaced))
                .multilineTextAlignment(.center)
                .focused($focused)
                .padding(.vertical, 8)
                .background(Color.white.opacity(0.08), in: RoundedRectangle(cornerRadius: 12))
                .onChange(of: code) { _, newValue in
                    let digits = String(newValue.filter(\.isNumber).prefix(6))
                    if digits != newValue { code = digits }
                    if digits.count == 6, !connection.pairingBusy { connection.pair(code: digits) }
                }
            if let error = connection.pairingError {
                Text(error).font(.footnote).foregroundStyle(.orange).multilineTextAlignment(.center)
            }
            Button {
                connection.pair(code: code)
            } label: {
                if connection.pairingBusy { ProgressView() } else { Text("Pair").frame(maxWidth: .infinity) }
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .disabled(code.count != 6 || connection.pairingBusy)
        }
        .padding(24)
        .frame(maxWidth: 360)
        .background(Color(white: 0.12), in: RoundedRectangle(cornerRadius: 22))
        .padding()
        .onAppear { focused = true }
        .onChange(of: connection.pairingError) { _, error in
            if error != nil { code = "" }
        }
    }
}

// MARK: - Keyboard and control bar

private struct KeyboardBar: View {
    @Binding var draft: String
    var focus: FocusState<Bool>.Binding
    let onSend: () -> Void
    let onReturn: () -> Void
    let onBackspace: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            TextField("Type to Claude", text: $draft)
                .focused(focus)
                .submitLabel(.return)
                .onSubmit(onReturn)
                .textFieldStyle(.plain)
                .padding(.horizontal, 12)
                .padding(.vertical, 9)
                .background(Color.white.opacity(0.1), in: Capsule())
            Button(action: onBackspace) {
                Image(systemName: "delete.left").font(.system(size: 18)).frame(width: 36, height: 36)
            }
            .accessibilityLabel("Backspace")
            Button("Send", action: onSend)
                .buttonStyle(.borderedProminent)
                .disabled(draft.isEmpty)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(Color(white: 0.08))
    }
}

private struct ControlBar: View {
    let connection: MirrorConnection
    @Binding var showKeyboard: Bool
    let onDisconnect: () -> Void

    var body: some View {
        HStack(spacing: 0) {
            BarButton(symbol: "keyboard", title: "Keyboard", highlighted: showKeyboard) { showKeyboard.toggle() }
            BarButton(symbol: "escape", title: "Esc") { connection.key("esc") }
            VStack(spacing: 3) {
                PasteButton(payloadType: String.self) { strings in
                    let text = strings.joined(separator: "\n")
                    Task { @MainActor in connection.type(text) }
                }
                .labelStyle(.iconOnly)
                .buttonBorderShape(.capsule)
                .controlSize(.small)
                .tint(Color(white: 0.3))
                Text("Paste").font(.caption2).foregroundStyle(.white)
            }
            .frame(maxWidth: .infinity)
            BarButton(symbol: "iphone", title: "Phone mode", highlighted: connection.phoneMode) {
                connection.setPhoneMode(!connection.phoneMode)
            }
            BarButton(symbol: "xmark.circle", title: "Disconnect", tint: .red, action: onDisconnect)
        }
        .padding(.top, 8)
        .padding(.bottom, 4)
        .background(Color(white: 0.06))
    }
}

private struct BarButton: View {
    let symbol: String
    let title: String
    var highlighted = false
    var tint: Color = .white
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            VStack(spacing: 3) {
                Image(systemName: symbol).font(.system(size: 20)).frame(height: 26)
                Text(title).font(.caption2).lineLimit(1).minimumScaleFactor(0.8)
            }
            .frame(maxWidth: .infinity)
            .foregroundStyle(highlighted ? Color.accentColor : tint)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(title)
    }
}
