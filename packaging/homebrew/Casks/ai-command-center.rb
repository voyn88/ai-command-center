# Homebrew cask for the unsigned-first distribution channel (#197).
#
# Lives here as the source of truth; publish by copying into the tap repo
# (dimastov-lab/homebrew-tap, path Casks/ai-command-center.rb) after each
# desktop-v* release and replacing `version`/`sha256` with the released values
# from SHA256SUMS.txt. Users install with:
#
#   brew tap dimastov-lab/tap
#   brew trust dimastov-lab/tap        # Homebrew 6: third-party taps need trust
#   brew install --cask ai-command-center
#   xattr -dr com.apple.quarantine "/Applications/AI Command Center.app"
#
# The app is not signed/notarized yet, so the quarantine attribute must be
# removed after install (Homebrew 6 dropped --no-quarantine), or right-click →
# Open on first launch. See docs/desktop/INSTALL_UNSIGNED.md.
cask "ai-command-center" do
  version "0.1.0"
  sha256 "c9fc32636d847754c2d8212d44fef463705ba85cefa059ed11bf0065b305b3b2"

  url "https://github.com/dimastov-lab/ai-command-center/releases/download/desktop-v#{version}/AI-Command-Center-macos-arm64.zip"
  name "AI Command Center"
  desc "AI Command Center desktop shell (unsigned build)"
  homepage "https://github.com/dimastov-lab/ai-command-center"

  depends_on arch: :arm64

  app "AI Command Center.app"

  caveats <<~EOS
    Эта сборка не подписана и не нотаризована Apple.
    Homebrew 6 требует доверить tap перед установкой:
      brew trust dimastov-lab/tap
    После установки снимите карантин (флага --no-quarantine больше нет):
      xattr -dr com.apple.quarantine "/Applications/AI Command Center.app"
    Либо при первом запуске: правый клик по приложению → «Открыть».
  EOS
end
