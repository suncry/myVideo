"""Windows Hello desktop interop, bound to the visible Qt HWND.

The ABI interface IDs and method slots come from Microsoft's WinSDK headers.
Only a verified result grants access. Windows credentials are the fallback when
Hello is unavailable; their logon token must identify the current Windows user.
No PIN/password, token, or authentication result is persisted.
"""
from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W
import uuid

HR=C.c_int32


class GUID(C.Structure):
    _fields_=[('data1',C.c_uint32),('data2',C.c_uint16),('data3',C.c_uint16),('data4',C.c_ubyte*8)]
    @classmethod
    def parse(cls,value):return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


def method(obj,index,result,*arguments):
    table=C.cast(obj,C.POINTER(C.POINTER(C.c_void_p))).contents
    return C.WINFUNCTYPE(result,C.c_void_p,*arguments)(table[index])


def checked(value):
    if value<0:raise OSError(f'Windows authentication HRESULT 0x{value & 0xffffffff:08x}')


class HelloAPI:
    def __init__(self):
        self.lib=C.WinDLL('combase');self.initialized=False
        self.lib.RoInitialize.argtypes=[C.c_uint32];self.lib.RoInitialize.restype=HR
        result=self.lib.RoInitialize(0)
        if result not in (0,1,-2147417850):checked(result)
        self.initialized=result in (0,1)
        self.lib.WindowsCreateString.argtypes=[W.LPCWSTR,C.c_uint32,C.POINTER(C.c_void_p)];self.lib.WindowsCreateString.restype=HR
        self.lib.WindowsDeleteString.argtypes=[C.c_void_p];self.lib.WindowsDeleteString.restype=HR
        self.lib.RoGetActivationFactory.argtypes=[C.c_void_p,C.POINTER(GUID),C.POINTER(C.c_void_p)];self.lib.RoGetActivationFactory.restype=HR

    def string(self,value):
        handle=C.c_void_p();checked(self.lib.WindowsCreateString(value,len(value.encode('utf-16-le'))//2,C.byref(handle)));return handle

    def request(self,hwnd):
        name=self.string('Windows.Security.Credentials.UI.UserConsentVerifier')
        message=self.string('验证后进入影库隐私模式')
        factory=C.c_void_p();operation=C.c_void_p();info=C.c_void_p()
        try:
            iid=GUID.parse('39e050c3-4e74-441a-8dc0-b81104df949c')
            checked(self.lib.RoGetActivationFactory(name,C.byref(iid),C.byref(factory)))
            # IInspectable(0..5), RequestVerificationForWindowAsync(6).
            async_iid=GUID.parse('fd596ffd-2318-558f-9dbe-d21df43764a5')
            checked(method(factory,6,HR,W.HWND,C.c_void_p,C.POINTER(GUID),C.POINTER(C.c_void_p))(factory,hwnd,message,C.byref(async_iid),C.byref(operation)))
            info_iid=GUID.parse('00000036-0000-0000-c000-000000000046')
            checked(method(operation,0,HR,C.POINTER(GUID),C.POINTER(C.c_void_p))(operation,C.byref(info_iid),C.byref(info)))
            return operation,info
        except BaseException:
            self.release(info);self.release(operation);raise
        finally:
            self.release(factory);self.lib.WindowsDeleteString(message);self.lib.WindowsDeleteString(name)

    def status(self,info):
        status=C.c_int32();checked(method(info,7,HR,C.POINTER(C.c_int32))(info,C.byref(status)));return status.value

    def result(self,operation):
        value=C.c_int32();checked(method(operation,8,HR,C.POINTER(C.c_int32))(operation,C.byref(value)));return value.value

    def cancel(self,info):
        if info:method(info,9,HR)(info)

    @staticmethod
    def release(obj):
        if obj:method(obj,2,C.c_uint32)(obj)

    def close(self):
        if self.initialized:self.lib.RoUninitialize();self.initialized=False


def verify_windows_credentials(hwnd):
    """Verify the current user's native credentials, without saving them."""
    class CREDUI_INFO(C.Structure):
        _fields_=[('size',W.DWORD),('parent',W.HWND),('message',W.LPCWSTR),('caption',W.LPCWSTR),('banner',W.HBITMAP)]
    class TOKEN_USER(C.Structure):
        _fields_=[('sid',C.c_void_p),('attributes',W.DWORD)]
    ui=C.WinDLL('credui',use_last_error=True);adv=C.WinDLL('advapi32',use_last_error=True);kernel=C.WinDLL('kernel32',use_last_error=True);ole=C.WinDLL('ole32')
    ui.CredUIPromptForWindowsCredentialsW.argtypes=[C.POINTER(CREDUI_INFO),W.DWORD,C.POINTER(W.ULONG),C.c_void_p,W.ULONG,C.POINTER(C.c_void_p),C.POINTER(W.ULONG),C.POINTER(W.BOOL),W.DWORD]
    ui.CredUIPromptForWindowsCredentialsW.restype=W.DWORD
    ui.CredUnPackAuthenticationBufferW.argtypes=[W.DWORD,C.c_void_p,W.DWORD,W.LPWSTR,C.POINTER(W.DWORD),W.LPWSTR,C.POINTER(W.DWORD),W.LPWSTR,C.POINTER(W.DWORD)];ui.CredUnPackAuthenticationBufferW.restype=W.BOOL
    adv.LogonUserW.argtypes=[W.LPCWSTR,W.LPCWSTR,W.LPCWSTR,W.DWORD,W.DWORD,C.POINTER(W.HANDLE)];adv.LogonUserW.restype=W.BOOL
    adv.OpenProcessToken.argtypes=[W.HANDLE,W.DWORD,C.POINTER(W.HANDLE)];adv.OpenProcessToken.restype=W.BOOL
    adv.GetTokenInformation.argtypes=[W.HANDLE,W.DWORD,C.c_void_p,W.DWORD,C.POINTER(W.DWORD)];adv.GetTokenInformation.restype=W.BOOL
    adv.EqualSid.argtypes=[C.c_void_p,C.c_void_p];adv.EqualSid.restype=W.BOOL
    kernel.GetCurrentProcess.restype=W.HANDLE;kernel.CloseHandle.argtypes=[W.HANDLE]
    ole.CoTaskMemFree.argtypes=[C.c_void_p]
    info=CREDUI_INFO(C.sizeof(CREDUI_INFO),hwnd,'验证当前 Windows 用户后进入隐私模式','影库',None)
    package=W.ULONG();buffer=C.c_void_p();length=W.ULONG();save=W.BOOL(False)
    user=C.create_unicode_buffer(1024);domain=C.create_unicode_buffer(1024);password=C.create_unicode_buffer(1024)
    current=W.HANDLE();token=W.HANDLE()
    try:
        # Enumerate the current user; no secure-desktop switch or save checkbox.
        if ui.CredUIPromptForWindowsCredentialsW(C.byref(info),0,C.byref(package),None,0,C.byref(buffer),C.byref(length),C.byref(save),0x200):return False
        u=W.DWORD(1024);d=W.DWORD(1024);p=W.DWORD(1024)
        if not ui.CredUnPackAuthenticationBufferW(1,buffer,length,user,C.byref(u),domain,C.byref(d),password,C.byref(p)):return False
        if not adv.LogonUserW(user,domain if domain.value else None,password,2,0,C.byref(token)):return False
        if not adv.OpenProcessToken(kernel.GetCurrentProcess(),8,C.byref(current)):return False
        def sid(handle):
            size=W.DWORD();adv.GetTokenInformation(handle,1,None,0,C.byref(size))
            if not size.value:raise OSError('TokenUser unavailable')
            storage=C.create_string_buffer(size.value)
            if not adv.GetTokenInformation(handle,1,storage,size,C.byref(size)):raise OSError('TokenUser unavailable')
            return storage,C.cast(storage,C.POINTER(TOKEN_USER)).contents.sid
        a,sa=sid(current);b,sb=sid(token)
        return bool(adv.EqualSid(sa,sb))
    finally:
        for value in (user,domain,password):C.memset(C.addressof(value),0,C.sizeof(value))
        if buffer:C.memset(buffer,0,length.value);ole.CoTaskMemFree(buffer)
        if current:kernel.CloseHandle(current)
        if token:kernel.CloseHandle(token)


class WindowsAuthBridge:
    def __init__(self,api_factory=HelloAPI,fallback=verify_windows_credentials):
        self.factory=api_factory;self.fallback=fallback;self.hwnd=0;self.api=None;self.operation=None;self.info=None;self.outcome=0
    def set_parent_window(self,parent):self.hwnd=int(parent.winId()) if parent is not None else 0
    def _fallback(self):
        try:self.outcome=1 if self.fallback(self.hwnd) else -1
        except (OSError,AttributeError):self.outcome=-2
    def yingku_auth_begin(self):
        if not self.hwnd:self.outcome=-2;return -2
        try:
            self.api=self.factory();self.operation,self.info=self.api.request(self.hwnd)
        except (OSError,AttributeError):self._fallback()
        return 0
    def yingku_auth_status(self):
        if self.outcome:return self.outcome
        try:
            status=self.api.status(self.info)
            if status==0:return 0
            if status!=1:self.outcome=-1;return self.outcome
            result=self.api.result(self.operation)
            if result==0:self.outcome=1
            elif result in (1,2,3):self._fallback()
            else:self.outcome=-1
        except (OSError,AttributeError):self.outcome=-2
        return self.outcome
    def yingku_auth_cancel(self):
        if self.api:
            try:
                if self.info and not self.outcome:self.api.cancel(self.info)
            finally:
                self.api.release(self.info);self.api.release(self.operation);self.api.close()
        self.api=None;self.info=None;self.operation=None
