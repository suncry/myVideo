import os,subprocess,sys,unittest
from unittest import mock
import windows_auth as auth,process_utils


class WindowsAuthenticationTests(unittest.TestCase):
    def bridge(self,result=0,status=1):
        api=mock.Mock();api.request.return_value=('operation','info');api.status.return_value=status;api.result.return_value=result
        fallback=mock.Mock(return_value=False);bridge=auth.WindowsAuthBridge(lambda:api,fallback);bridge.hwnd=123
        return bridge,api,fallback
    def test_verified_system_result_is_required(self):
        bridge,api,fallback=self.bridge();self.assertEqual(bridge.yingku_auth_begin(),0);self.assertEqual(bridge.yingku_auth_status(),1);api.request.assert_called_once_with(123);fallback.assert_not_called()
    def test_cancel_does_not_prompt_for_credentials(self):
        for result in (4,5,6):
            bridge,api,fallback=self.bridge(result);bridge.yingku_auth_begin();self.assertEqual(bridge.yingku_auth_status(),-1);fallback.assert_not_called()
    def test_unavailable_hello_can_use_current_user_credentials(self):
        bridge,api,fallback=self.bridge(2);fallback.return_value=True;bridge.yingku_auth_begin();self.assertEqual(bridge.yingku_auth_status(),1);fallback.assert_called_once_with(123)
    def test_unsupported_windows_can_fall_back_but_not_bypass_authentication(self):
        bridge,api,fallback=self.bridge();api.request.side_effect=OSError('unsupported');bridge.yingku_auth_begin();self.assertEqual(bridge.yingku_auth_status(),-1)
    def test_pending_operation_is_cancelled_and_released(self):
        bridge,api,fallback=self.bridge(status=0);bridge.yingku_auth_begin();self.assertEqual(bridge.yingku_auth_status(),0);bridge.yingku_auth_cancel();api.cancel.assert_called_once_with('info');self.assertEqual([c.args[0] for c in api.release.call_args_list],['info','operation']);api.close.assert_called_once()
    def test_error_and_missing_window_never_unlock(self):
        bridge,api,fallback=self.bridge(status=3);bridge.yingku_auth_begin();self.assertEqual(bridge.yingku_auth_status(),-1)
        bridge.hwnd=0;self.assertEqual(bridge.yingku_auth_begin(),-2)
    def test_credentials_for_another_user_are_rejected_and_buffer_is_erased(self):
        import ctypes as C
        ui=mock.Mock();adv=mock.Mock();kernel=mock.Mock();ole=mock.Mock();packed=C.create_string_buffer(b'synthetic-credential')
        def prompt(info,error,package,inputbuf,inputlen,out,length,save,flags):
            out._obj.value=C.addressof(packed);length._obj.value=C.sizeof(packed);return 0
        def unpack(flags,buf,size,user,u,domain,d,password,p):user.value='other';password.value='synthetic';return True
        def login(user,domain,password,kind,provider,out):out._obj.value=10;return True
        def current(process,access,out):out._obj.value=20;return True
        def token(handle,kind,storage,size,needed):
            needed._obj.value=32
            if storage is not None:C.cast(storage,C.POINTER(C.c_void_p))[0]=handle.value
            return storage is not None
        ui.CredUIPromptForWindowsCredentialsW.side_effect=prompt;ui.CredUnPackAuthenticationBufferW.side_effect=unpack;adv.LogonUserW.side_effect=login;adv.OpenProcessToken.side_effect=current;adv.GetTokenInformation.side_effect=token;adv.EqualSid.side_effect=lambda a,b:a==b
        with mock.patch.object(C,'WinDLL',side_effect=lambda name,**kw:{'credui':ui,'advapi32':adv,'kernel32':kernel,'ole32':ole}[name],create=True):
            self.assertFalse(auth.verify_windows_credentials(123))
        self.assertEqual(packed.raw,b'\0'*C.sizeof(packed));ole.CoTaskMemFree.assert_called_once();self.assertEqual(kernel.CloseHandle.call_count,2)
    def test_process_check_does_not_send_windows_signal_zero(self):
        kernel=mock.Mock();kernel.OpenProcess.return_value=7
        kernel.GetExitCodeProcess.side_effect=lambda handle,out:setattr(out._obj,'value',259) or True
        with mock.patch.object(process_utils.sys,'platform','win32'),mock.patch.object(process_utils.ctypes,'WinDLL',return_value=kernel,create=True),mock.patch.object(process_utils.os,'kill') as kill:
            self.assertTrue(process_utils.process_alive(123));kill.assert_not_called();kernel.CloseHandle.assert_called_once_with(7)
    @unittest.skipUnless(sys.platform=='win32','Windows integration')
    def test_real_windows_process_poll_preserves_running_child(self):
        child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(5)'])
        try:self.assertTrue(process_utils.process_alive(child.pid));self.assertIsNone(child.poll())
        finally:child.terminate();child.wait(timeout=10)
        self.assertFalse(process_utils.process_alive(child.pid))


if __name__=='__main__':unittest.main()
