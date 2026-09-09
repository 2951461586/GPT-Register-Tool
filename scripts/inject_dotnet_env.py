#!/usr/bin/env python
"""
Inject Windows shell environment variables that are normally set by winlogon.exe
during interactive desktop sessions but missing in sandboxed/non-interactive contexts.

NuGet (NuGetEnvironment.GetFolderPath) reads these directly from the environment
without falling back to the Win32 API. When they are absent, ConfigurationDefaults
throws ArgumentNullException at Path.Combine(null, ...).

Usage: python inject_dotnet_env.py [command args...]
  - With args: runs the command with the injected environment
  - Without args: prints the environment variables to stdout
"""
import os, sys, subprocess

ENV_VARS = {
    'APPDATA': r'C:\Users\29514\AppData\Roaming',
    'LOCALAPPDATA': r'C:\Users\29514\AppData\Local',
    'USERPROFILE': r'C:\Users\29514',
    'PROGRAMFILES': r'C:\Program Files',
    'PROGRAMFILES(X86)': r'C:\Program Files (x86)',
    'PROGRAMDATA': r'C:\ProgramData',
    'COMMONPROGRAMFILES': r'C:\Program Files\Common Files',
    'COMMONPROGRAMFILES(X86)': r'C:\Program Files (x86)\Common Files',
    'HOMEDRIVE': 'C:',
    'HOMEPATH': r'\Users\29514',
}

def build_env():
    env = os.environ.copy()
    for k, v in ENV_VARS.items():
        env.setdefault(k, v)
    env.setdefault('DOTNET_ROOT', r'C:\Users\29514\.dotnet')
    env.setdefault('DOTNET_MULTILEVEL_LOOKUP', '0')
    return env

if __name__ == '__main__':
    if len(sys.argv) > 1:
        env = build_env()
        result = subprocess.run(sys.argv[1:], env=env)
        sys.exit(result.returncode)
    else:
        for k, v in ENV_VARS.items():
            print(f'{k}={v}')
