%global debug_package %{nil}

Name:           splitrandr
Version:        0.1.0
Release:        1%{?dist}
Summary:        Monitor Layout Editor with Virtual Monitor Splitting

License:        GPL-3.0-or-later
URL:            https://github.com/DigitalCyberSoft/splitrandr
Source0:        %{name}-%{version}.tar.gz

BuildRequires:  gcc
BuildRequires:  python3-devel
BuildRequires:  pkgconfig(xrandr)
BuildRequires:  pkgconfig(xinerama)
BuildRequires:  pkgconfig(xcb-randr)
BuildRequires:  pkgconfig(x11)
BuildRequires:  desktop-file-utils
BuildRequires:  pkgconfig

Requires:       python3
Requires:       python3-gobject
Requires:       gtk3
Requires:       xrandr
Requires:       libXrandr

Recommends:     xapp
Recommends:     libappindicator-gtk3

%description
SplitRandR is a monitor layout editor based on ARandR that adds virtual
monitor splitting via a bundled fakexrandr library. It allows splitting
physical monitors into multiple virtual screens for window management.

%prep
%setup -q
cd fakexrandr && make clean || true

%build
cd fakexrandr && ./configure && make %{?_smp_mflags}

%install
# Python package
install -d %{buildroot}%{python3_sitelib}/splitrandr
install -d %{buildroot}%{python3_sitelib}/splitrandr/data
install -p -m 644 splitrandr/*.py %{buildroot}%{python3_sitelib}/splitrandr/
install -p -m 644 splitrandr/data/splitrandr.desktop %{buildroot}%{python3_sitelib}/splitrandr/data/

# Binary wrapper
install -d %{buildroot}%{_bindir}
install -p -m 755 bin/splitrandr %{buildroot}%{_bindir}/splitrandr

# Desktop file
install -d %{buildroot}%{_datadir}/applications
install -p -m 644 splitrandr/data/splitrandr.desktop %{buildroot}%{_datadir}/applications/splitrandr.desktop
desktop-file-validate %{buildroot}%{_datadir}/applications/splitrandr.desktop

# fakexrandr libraries
install -d %{buildroot}%{_prefix}/local/lib64
install -p -m 755 fakexrandr/libXrandr.so %{buildroot}%{_prefix}/local/lib64/libXrandr.so
ln -s libXrandr.so %{buildroot}%{_prefix}/local/lib64/libXrandr.so.2
ln -s libXrandr.so %{buildroot}%{_prefix}/local/lib64/libXinerama.so.1

%post
/sbin/ldconfig

%postun
/sbin/ldconfig

%files
%license splitrandr/meta.py
%doc README.md
%{python3_sitelib}/splitrandr/
%{_bindir}/splitrandr
%{_datadir}/applications/splitrandr.desktop
%{_prefix}/local/lib64/libXrandr.so
%{_prefix}/local/lib64/libXrandr.so.2
%{_prefix}/local/lib64/libXinerama.so.1

%changelog
* Sat Feb 07 2026 DigitalCyberSoft <digitalcybersoft@proton.me> - 0.1.0-1
- Initial RPM package
