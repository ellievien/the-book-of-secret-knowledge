import SwiftUI

/// Screen 1: computers running Reflect on this Wi-Fi.
struct HelperListView: View {
    @Environment(AppModel.self) private var model
    @State private var showManual = false

    var body: some View {
        NavigationStack {
            List {
                Section {
                    if model.browser.helpers.isEmpty {
                        HStack(spacing: 12) {
                            ProgressView()
                            Text("Looking for computers running Reflect\u{2026}")
                                .foregroundStyle(.secondary)
                        }
                    }
                    ForEach(model.browser.helpers) { helper in
                        Button {
                            model.connect(to: helper)
                        } label: {
                            HelperRow(name: helper.name, os: helper.os,
                                      detail: CredentialStore.has(helperID: helper.id) ? "Paired" : "Tap to pair")
                        }
                    }
                } header: {
                    Text("On this Wi-Fi")
                } footer: {
                    Text("Start Reflect on your computer (Reflect.cmd on Windows, run.sh on Mac). The iPhone and the computer must be on the same Wi-Fi.")
                }

                if let problem = model.browser.problem {
                    Section {
                        Label(problem, systemImage: "exclamationmark.triangle")
                            .foregroundStyle(.orange)
                    }
                }

                let offline = model.store.saved.filter { saved in !model.browser.helpers.contains { $0.id == saved.id } }
                if !offline.isEmpty {
                    Section("Recent") {
                        ForEach(offline) { saved in
                            Button {
                                model.connect(saved: saved)
                            } label: {
                                HelperRow(name: saved.name, os: saved.os, detail: "\(saved.host) \u{2014} not seen right now")
                            }
                        }
                        .onDelete { indexSet in
                            for index in indexSet { model.store.forget(id: offline[index].id) }
                        }
                    }
                }

                Section {
                    Button("Connect by address\u{2026}") { showManual = true }
                }
            }
            .navigationTitle("Reflect")
            .refreshable { model.browser.restart() }
            .sheet(isPresented: $showManual) {
                ManualConnectSheet { host, port in
                    showManual = false
                    model.connect(host: host, port: port)
                }
                .presentationDetents([.medium])
            }
        }
    }
}

private struct HelperRow: View {
    let name: String
    let os: String
    let detail: String

    var body: some View {
        HStack(spacing: 14) {
            Image(systemName: os == "macos" ? "laptopcomputer" : "desktopcomputer")
                .font(.title2)
                .foregroundStyle(.tint)
                .frame(width: 36)
            VStack(alignment: .leading, spacing: 2) {
                Text(name).font(.body.weight(.semibold)).foregroundStyle(.primary)
                Text(detail).font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Image(systemName: "chevron.right").font(.caption).foregroundStyle(.tertiary)
        }
        .padding(.vertical, 4)
    }
}

private struct ManualConnectSheet: View {
    let onConnect: (String, Int) -> Void
    @State private var host = ""
    @State private var port = String(Wire.defaultPort)

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Computer's IP address, e.g. 192.168.1.20", text: $host)
                        .keyboardType(.numbersAndPunctuation)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    TextField("Port", text: $port)
                        .keyboardType(.numberPad)
                } footer: {
                    Text("Use this if the computer does not show up automatically. The address and port are printed when Reflect starts.")
                }
            }
            .navigationTitle("Connect by address")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Connect") {
                        onConnect(host.trimmingCharacters(in: .whitespaces), Int(port) ?? Wire.defaultPort)
                    }
                    .disabled(host.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
        }
    }
}
