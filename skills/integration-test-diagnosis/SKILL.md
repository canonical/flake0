---
name: integration-test-diagnosis
description: Diagnosis of a failing integration test. Use this skill to investigate and classify the root cause of a failing integration test.
---

Identify the root cause of a failing integration test, to classify whether the failure is due to a code issue, a test issue, or an environmental issue.

## Procedure
1. **Gather Information**: Collect all relevant information about the failing integration test. All what you need will be available locally, this includes code source change, test logs, environment configuration and metrics about resources.

2. **Analyze Test Logs**: Review the test logs and error messages to identify any patterns or specific errors that can indicate the nature of the failure.

3. **Investigate Test Environment**: Check the test environment for any issues that may be affecting the test execution. This includes verifying:
   - Environment variables and configurations
   - Dependency versions and compatibility
   - Resource availability (CPU, memory, disk space)
   - Network connectivity and access to required services

4. **Check Recent Changes**: Examine recent code changes or deployments that may have introduced the failure. Look for:
   - Code commits that affect the functionality being tested
   - Changes in dependencies or configurations
   - Any recent merges or pull requests

5. **Hypothesize Root Cause**: Based on the gathered information and analysis, hypothesize whether the failure is due to:
   - **Code Issue**: A bug or defect in the code that causes the test to fail.
   - **Test Issue**: A problem with the test itself, such as incorrect assertions, setup issues, or outdated test cases.
   - **Environmental Issue**: An issue related to the test environment, such as missing dependencies, configuration errors, or resource limitations.
   - **Other**: If the failure does not fit into the above categories, consider other potential causes, such as external service failures or intermittent issues.

6. **Document Findings**: Record your findings, including the identified root cause, any supporting evidence, and recommendations for resolving the issue.
