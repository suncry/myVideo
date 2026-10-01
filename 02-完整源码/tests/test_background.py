import gc,threading,time,unittest
from PySide6.QtCore import QCoreApplication,QEvent,QThread,QThreadPool
from PySide6.QtWidgets import QApplication,QWidget
from background_tasks import TaskRunner

class BackgroundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.qt=QApplication.instance() or QApplication([])
    def drain(self,predicate,seconds=5):
        end=time.monotonic()+seconds
        while time.monotonic()<end and not predicate():
            self.qt.processEvents();time.sleep(.002)
        self.qt.processEvents()
        self.assertTrue(predicate())
    def test_fast_success_and_failure_callbacks_stay_on_gui_thread(self):
        owner=QWidget();runner=TaskRunner(owner)
        main=threading.get_ident();results=[];errors=[];workers=[]
        def work(i):
            workers.append(threading.get_ident())
            if i%3==0:raise ValueError(str(i))
            return i
        def success(value):results.append((value,threading.get_ident(),QThread.currentThread()==self.qt.thread()))
        def error(value):errors.append((value,threading.get_ident(),QThread.currentThread()==self.qt.thread()))
        for i in range(180):runner.start(lambda i=i:work(i),success,error)
        gc.collect()
        self.drain(lambda:len(results)+len(errors)==180)
        self.assertEqual(len(results),120);self.assertEqual(len(errors),60)
        self.assertTrue(all(tid==main and qt for _,tid,qt in results+errors))
        self.assertTrue(all(tid!=main for tid in workers));self.assertEqual(runner.pending_count,0)
        owner.deleteLater();self.qt.processEvents()
    def test_result_can_submit_followup_work(self):
        owner=QWidget();runner=TaskRunner(owner);seen=[]
        def receive(n):
            seen.append(n)
            if n<20:runner.start(lambda:n+1,receive)
        runner.start(lambda:0,receive)
        self.drain(lambda:len(seen)==21)
        self.assertEqual(seen,list(range(21)));self.assertEqual(runner.pending_count,0)
        owner.deleteLater();self.qt.processEvents()
    def test_destroyed_window_discards_inflight_results(self):
        owner=QWidget();runner=TaskRunner(owner);seen=[];release=threading.Event()
        runner.start(lambda:release.wait(2),seen.append)
        owner.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        release.set();QThreadPool.globalInstance().waitForDone(3000)
        self.qt.processEvents();self.assertEqual(seen,[])
