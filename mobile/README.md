# RandomGenerals AI — iOS / Android shell

Capacitor wraps the live site in a native app. This directory holds
everything that can be prepared on Windows. The parts that need a Mac are
marked, and so are the parts that cost money.

    npm install
    npx cap add ios        # macOS only
    npx cap sync ios
    npx cap open ios       # opens Xcode

## What you cannot avoid

**An Apple Developer account is $99 a year.** There is no free tier for
App Store distribution. A free Apple ID can build to your own iPhone for
seven days at a time, which is enough to test but not to publish.

**You need a Mac.** Xcode is macOS-only and there is no supported way
around it. Cloud Mac builders (Codemagic, Expo EAS, MacStadium) exist and
have free tiers small enough to publish an app that changes rarely.

**Android costs $25, once, forever.** If the goal is "people can install
my app", Google Play is a twentieth of the price and has no hardware
requirement. Worth doing first.

## Three review rules that will decide whether this is accepted

These are the ones that reject apps like this one. None is a formality.

### 4.2 Minimum Functionality

A web page in a wrapper gets rejected. The app has to do something a
browser cannot. What is already configured here helps — splash screen,
native keyboard handling, status bar, haptics, the share sheet, and an
offline screen that ships inside the binary — but the honest position is
that a reviewer may still call this a website. Push notifications and a
share extension are the usual things that settle it.

### 4.8 Sign in with Apple

An app offering a third-party login **must** also offer Sign in with
Apple. This app has Google sign-in, so this is not optional. It is real
work on both sides: an Apple developer key, a new OAuth route, and an
account-linking path for people who already signed up with Google.

### 3.1.1 In-App Purchase

Digital subscriptions sold inside an iOS app must use Apple's system,
and Apple takes **30%** — or 15% under the Small Business Program, which
you would qualify for. Paddle checkout cannot be used inside the app for
the Pro plan.

On a $1.99 subscription that is 30¢ a month to Apple at the reduced rate.
The usual approach is to let people subscribe on the website and simply
not mention or link to it from the app, which Apple permits but polices
narrowly.

## What is ready here

| File | Purpose |
| --- | --- |
| `capacitor.config.json` | app id, name, the live URL, offline fallback |
| `www/offline.html` | the no-connection screen, bundled in the binary |
| `ios-icons/` | 13 sizes, RGB with no alpha, generated from the logo |

Regenerate the icons after any logo change:

    python ../tools/make_icons.py --ios

The icons are deliberately flat RGB with square corners. An alpha channel
is an automatic rejection from App Store Connect, and iOS rounds the
corners itself — supplying rounded corners produces a double-rounded
icon.

## Before submitting

- [ ] `NSCameraUsageDescription` etc. in `Info.plist` for any permission
      the app requests. A missing usage string is a crash, not a warning.
- [ ] A privacy manifest (`PrivacyInfo.xcprivacy`) — required since 2024.
- [ ] App Privacy answers in App Store Connect matching `/privacy`.
- [ ] Screenshots at 6.7" and 6.5". Simulator screenshots are accepted.
- [ ] An account for the reviewer to sign in with, or they cannot test it.
- [ ] Age rating. The site already refuses under-16s; keep the two
      consistent or the rating questionnaire contradicts your own policy.

## The cheaper route you already have

The site is an installable PWA. On an iPhone: Share → Add to Home Screen.
It gets its own icon, opens without browser chrome, and works offline —
no $99, no Mac, no review. It cannot do push notifications on iOS and it
is not in search results on the App Store, which is the real trade.
