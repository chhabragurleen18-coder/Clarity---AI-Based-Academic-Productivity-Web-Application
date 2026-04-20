import os
import io
import json
import unittest
import shutil
from app import app, load_data, save_data

class ClarityTests(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        app.secret_key = 'test_key'
        self.client = app.test_client()
        os.makedirs('data', exist_ok=True)
        
    def test_login_flow(self):
        import uuid
        uname = "u_" + uuid.uuid4().hex
        
        # Register user
        res = self.client.post('/api/auth/register', json={'username': uname, 'password': 'testpassword'})
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertTrue(data['success'])
        self.assertTrue(data['is_new'])
        
        # Login user
        res = self.client.post('/api/auth/login', json={'username': uname, 'password': 'testpassword'})
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertTrue(data['success'])
        # api_login doesn't return is_new
        
        # Check data isolation
        self.assertTrue(os.path.exists('data/users.json'))
        with open('data/users.json') as f:
            users = json.load(f)
            self.assertIn(uname, users)

    def test_timetable_overflow(self):
        import uuid
        uname = "t_" + uuid.uuid4().hex
        res = self.client.post('/api/auth/register', json={'username': uname, 'password': 'password123'})
        self.assertEqual(res.status_code, 200, f"Register failed: {res.data}")
        
        # Add a huge task
        res = self.client.post('/api/tasks', json={
            'title': 'Huge Task',
            'estimated_time': 300,  # 5 hours
            'priority': 'High',
            'study_type': 'reading',
            'deadline': '2030-01-01'
        })
        
        # Add a tiny free slot in onboarding
        self.client.post('/api/onboard', json={
            'wake': '08:00',
            'sleep': '22:00',
            'free_slots': [{'from': '10:00', 'to': '11:00'}],
            'commitments': []
        })
        
        res = self.client.post('/api/generate_timetable')
        if res.status_code != 200:
            print("Generate timetable failed:", res.data)
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertTrue(data.get('success', False))
        tt = data['timetable']
        
        # Verify carry_forward exists on some tasks
        carry_forwards = [t for t in tt if t.get('carry_forward')]
        self.assertTrue(len(carry_forwards) > 0)

    def test_upload_invalid_pdf(self):
        import uuid
        uname = "p_" + uuid.uuid4().hex
        res = self.client.post('/api/auth/register', json={'username': uname, 'password': 'password123'})
        self.assertEqual(res.status_code, 200, f"Register failed: {res.data}")
        
        # Create fake invalid PDF
        data = {
            'file': (io.BytesIO(b"Not a real PDF!"), 'test.pdf')
        }
        res = self.client.post('/api/upload_calendar', data=data, content_type='multipart/form-data')
        if res.status_code == 401:
            print("Upload PDF auth failed:", res.data)
        self.assertEqual(res.status_code, 400)
        res_data = json.loads(res.data)
        self.assertIn("error", res_data)

if __name__ == '__main__':
    unittest.main()
