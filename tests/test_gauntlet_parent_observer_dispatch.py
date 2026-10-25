"""Independent adversarial contract for code-constant observation."""
import unittest
from hermes_nerve import provenance

class ObserverDispatchTests(unittest.TestCase):
    def test_constants_cannot_invoke_custom_class_hook(self):
        calls = []
        class Constant:
            def __getattribute__(self, name):
                if name == "__class__":
                    calls.append(name)
                    return object
                return object.__getattribute__(self, name)
        code = (lambda: None).__code__.replace(co_consts=(None, Constant()))
        result = provenance._all_code_objects(code)
        self.assertEqual(len(result), 1)
        self.assertIs(result[0], code)
        self.assertEqual(calls, [])

    def test_legitimate_nested_code_objects_still_traverse(self):
        def outer():
            def inner():
                return "retained policy data"
            return inner
        code = outer.__code__
        nested = next(value for value in code.co_consts if type(value) is type(code))
        result = provenance._all_code_objects(code)
        self.assertEqual(len(result), 2)
        self.assertIs(result[0], code)
        self.assertIs(result[1], nested)

if __name__ == "__main__":
    unittest.main()
